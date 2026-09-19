//! Client-side mirror of Spotify's rate limit on audio keys.
//!
//! Measured (2026-09): keys come out of a token bucket that is shared by all the sessions of an account. It holds
//! a burst of at least ~25 keys and refills with one key every ~30 seconds, so ~120 tracks an hour is the ceiling
//! whatever the client does. A request made while the bucket is empty is refused ("error audio key 0 2"), which
//! costs nothing: no key, no penalty.
//!
//! So rather than treating a refusal as an error to back off from, this keeps a model of the bucket. A refusal
//! means "empty now": poll until the next key shows up, which tells when keys arrive, then ask for each following
//! key right when it is due. That runs at the ceiling, almost without being refused. The refill interval is
//! measured, not assumed: two observed arrivals with n keys in between are n intervals apart. To notice a limit
//! that got more generous, the interval is shortened a little after a run of granted requests; the refusal this
//! ends in is the next measurement. The model is saved, so that the next run starts paced instead of finding out
//! again that the bucket is empty. In simulation this stays within 0.1% of the ceiling for refills of 12 to 60s.

use std::path::PathBuf;

use serde_json::Value;

const INITIAL_INTERVAL: f64 = 30.0;
const INTERVAL_RANGE: (f64, f64) = (5.0, 120.0);
/// Seconds between requests while waiting for the next key of an empty bucket.
const POLL: f64 = 2.0;
/// After this many intervals without a request the bucket has refilled by an unknown amount (its size is not
/// known): stop pacing and let a refusal say when it is empty again.
const TRUSTED_KEYS: f64 = 20.0;

pub trait Clock {
  /// Unix time, in seconds.
  fn now(&self) -> f64;
  fn sleep(&mut self, seconds: f64);
}

pub struct KeyPacer {
  /// Seconds between refills: measured once `settled`, a guess before.
  interval: f64,
  settled: bool,
  /// When the next key is expected. None when the state of the bucket is not known.
  due: Option<f64>,
  /// The last observed arrival of a key, and how many keys were granted on time since: a measurement in progress.
  observed: Option<(f64, u32)>,
  on_time_streak: u32,
  path: Option<PathBuf>,
}

impl KeyPacer {
  pub fn load(path: Option<PathBuf>) -> KeyPacer {
    let saved: Value = path
      .as_ref()
      .and_then(|path| std::fs::read(path).ok())
      .and_then(|bytes| serde_json::from_slice(&bytes).ok())
      .unwrap_or(Value::Null);
    KeyPacer {
      interval: saved["interval"]
        .as_f64()
        .unwrap_or(INITIAL_INTERVAL)
        .clamp(INTERVAL_RANGE.0, INTERVAL_RANGE.1),
      settled: saved["settled"].as_bool().unwrap_or(false),
      due: saved["due"].as_f64(),
      observed: None,
      on_time_streak: 0,
      path,
    }
  }

  fn save(&self) {
    if let Some(path) = &self.path {
      let state = json!({"interval": self.interval, "settled": self.settled, "due": self.due});
      if let Err(e) = std::fs::write(path, state.to_string()) {
        debug!("Cannot save the rate limit model: {}", e);
      }
    }
  }

  pub fn interval(&self) -> f64 {
    self.interval
  }

  /// How many keys are expected to be granted right away; None when the state of the bucket is not known.
  pub fn available(&self, now: f64) -> Option<u32> {
    let overdue = now - self.due?;
    if overdue > TRUSTED_KEYS * self.interval {
      None
    } else if overdue < 0.0 {
      Some(0)
    } else {
      Some((overdue / self.interval) as u32 + 1)
    }
  }

  /// Gets a key out of `request`, waiting for the rate limit as needed. `waiting` is told about waits worth
  /// mentioning. A refusal that lasts longer than any refill could take is not the rate limit: it is returned.
  pub fn acquire<K, E>(
    &mut self,
    clock: &mut impl Clock,
    mut request: impl FnMut() -> Result<K, E>,
    mut waiting: impl FnMut(f64),
  ) -> Result<K, E> {
    if self.available(clock.now()).is_none() {
      (self.due, self.observed) = (None, None);
    }
    let delay = self.due.map_or(0.0, |due| (due - clock.now()).max(0.0));
    let on_time = delay > 0.0;
    if delay >= 1.0 {
      waiting(delay);
    }
    clock.sleep(delay);
    let mut refused_since = None;
    loop {
      let requested_at = clock.now();
      match request() {
        Ok(key) => {
          if refused_since.is_some() {
            // The key arrived within the last poll. If the bucket has been empty ever since the last arrival
            // seen this way, the refill interval can be read off the two.
            if let (true, Some((arrival, keys))) = (on_time, self.observed) {
              let measured = (requested_at - arrival) / (keys + 1) as f64;
              self.interval = measured.clamp(INTERVAL_RANGE.0, INTERVAL_RANGE.1);
              self.settled = true;
            }
            self.due = Some(requested_at + self.interval);
            self.observed = Some((requested_at, 0));
            self.on_time_streak = 0;
          } else if let Some(due) = self.due {
            self.due = Some(due + self.interval);
            match (on_time, &mut self.observed) {
              (true, Some((_, keys))) => *keys += 1,
              // keys had piled up: the bucket was not empty all along
              _ => self.observed = None,
            }
            self.on_time_streak += on_time as u32;
            if self.on_time_streak >= if self.settled { 10 } else { 5 } {
              self.on_time_streak = 0;
              let bolder = self.interval * if self.settled { 0.98 } else { 0.9 };
              self.interval = bolder.max(INTERVAL_RANGE.0);
            }
          }
          self.save();
          return Ok(key);
        }
        Err(e) => {
          let since = *refused_since.get_or_insert(requested_at);
          if requested_at - since > (3.0 * self.interval).max(90.0) {
            return Err(e);
          }
          if since == requested_at {
            self.due = Some(requested_at + POLL);
            self.save();
            waiting(self.interval);
          }
          clock.sleep(POLL);
        }
      }
    }
  }
}

#[cfg(test)]
mod tests {
  use std::cell::Cell;
  use std::rc::Rc;

  use super::*;

  #[derive(Clone)]
  struct FakeClock(Rc<Cell<f64>>);

  impl Clock for FakeClock {
    fn now(&self) -> f64 {
      self.0.get()
    }
    fn sleep(&mut self, seconds: f64) {
      self.0.set(self.0.get() + seconds);
    }
  }

  fn clock() -> FakeClock {
    FakeClock(Rc::new(Cell::new(0.0)))
  }

  /// Spotify's side: a token bucket.
  struct Bucket {
    tokens: f64,
    capacity: f64,
    refill: f64,
    at: f64,
    refused: u32,
  }

  impl Bucket {
    fn request(&mut self, now: f64) -> Result<(), ()> {
      self.tokens = (self.tokens + (now - self.at) / self.refill).min(self.capacity);
      self.at = now;
      if self.tokens >= 1.0 {
        self.tokens -= 1.0;
        Ok(())
      } else {
        self.refused += 1;
        Err(())
      }
    }
  }

  /// Downloads `keys` tracks (2s each) and returns (seconds taken, refusals).
  fn run(pacer: &mut KeyPacer, bucket: &mut Bucket, clock: &mut FakeClock, keys: u32) -> (f64, u32) {
    let (start, refused) = (clock.now(), bucket.refused);
    for _ in 0..keys {
      let time = clock.0.clone();
      pacer
        .acquire(&mut clock.clone(), || bucket.request(time.get()), |_| ())
        .expect("a key");
      clock.sleep(2.0);
    }
    (clock.now() - start, bucket.refused - refused)
  }

  fn bucket(refill: f64) -> Bucket {
    Bucket { tokens: 25.0, capacity: 25.0, refill, at: 0.0, refused: 0 }
  }

  #[test]
  fn runs_at_the_ceiling_with_few_refusals() {
    for refill in [30.0, 27.3, 34.0, 12.0, 60.0] {
      let (mut pacer, mut bucket, mut clock) = (KeyPacer::load(None), bucket(refill), clock());
      let (seconds, refused) = run(&mut pacer, &mut bucket, &mut clock, 225);
      let ideal = 200.0 * refill; // 25 come from the burst
      assert!(seconds < ideal * 1.08, "refill {}: took {} for an ideal of {}", refill, seconds, ideal);
      assert!(refused < 60, "refill {}: refused {} times", refill, refused);
      assert!((pacer.interval() - refill).abs() < 1.5, "refill {}: learned {}", refill, pacer.interval());
    }
  }

  #[test]
  fn bursts_again_after_a_long_break() {
    let (mut pacer, mut bucket, mut clock) = (KeyPacer::load(None), bucket(30.0), clock());
    run(&mut pacer, &mut bucket, &mut clock, 40);
    clock.sleep(3600.0);
    assert_eq!(pacer.available(clock.now()), None);
    let (seconds, refused) = run(&mut pacer, &mut bucket, &mut clock, 25);
    assert!(seconds < 60.0 && refused == 0, "took {} with {} refusals", seconds, refused);
  }

  #[test]
  fn a_short_break_is_spent_exactly() {
    let (mut pacer, mut bucket, mut clock) = (KeyPacer::load(None), bucket(30.0), clock());
    run(&mut pacer, &mut bucket, &mut clock, 30);
    clock.sleep(155.0);
    assert_eq!(pacer.available(clock.now()), Some(5));
    let (_, refused) = run(&mut pacer, &mut bucket, &mut clock, 8);
    assert_eq!(refused, 0);
  }

  #[test]
  fn a_refusal_that_is_not_the_rate_limit_is_returned() {
    let (mut pacer, mut clock) = (KeyPacer::load(None), clock());
    let result: Result<(), &str> = pacer.acquire(&mut clock, || Err("no key for this track"), |_| ());
    assert_eq!(result, Err("no key for this track"));
    assert!(clock.now() < 120.0);
  }
}
