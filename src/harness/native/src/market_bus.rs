//! Deterministic synchronous event queue and bus, ported from
//! `src/harness/core/events.py` and the native bus kernel.
//!
//! The queue is a binary heap ordered by `(timestamp, sequence, class
//! priority, feed ordinal)`. The bus dispatches events synchronously in one
//! deterministic loop and maintains a SHA-256 digest of the dispatched
//! identities, byte-identical to the Python bus. The clock is optional: when
//! present it advances to each dispatched timestamp.

use core::cell::RefCell;
use std::rc::Rc;

use crate::clock::{ClockError, SimClock};
use crate::market::EventType;
use crate::sha256::Sha256;

/// A shared, advanceable simulation clock.
pub type SharedClock = Rc<RefCell<SimClock>>;

/// Create a shared simulation clock positioned at `start_ns`.
pub fn shared_clock(start_ns: i128) -> SharedClock {
    Rc::new(RefCell::new(SimClock::new(start_ns)))
}

/// A checked failure from the event bus.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// Allocation failure.
    OutOfMemory,
    /// An event or envelope field was empty or malformed.
    InvalidArgument,
    /// The event timestamp is older than the clock head or last dispatch.
    StaleTimestamp,
    /// The event ordering key precedes the last dispatched key.
    RetrogradeOrderKey,
    /// The dispatch identity exceeded the fixed digest buffer.
    IdentityTooLong,
    /// The clock rejected a retrograde timestamp.
    RetrogradeTimestamp,
    /// The clock rejected a negative delta.
    NegativeDelta,
}

impl core::fmt::Display for Error {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        let message = match self {
            Self::OutOfMemory => "out of memory",
            Self::InvalidArgument => "invalid argument",
            Self::StaleTimestamp => "stale timestamp",
            Self::RetrogradeOrderKey => "retrograde order key",
            Self::IdentityTooLong => "dispatch identity too long",
            Self::RetrogradeTimestamp => "retrograde timestamp",
            Self::NegativeDelta => "negative delta",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for Error {}

impl From<ClockError> for Error {
    fn from(error: ClockError) -> Self {
        match error {
            ClockError::RetrogradeTimestamp => Self::RetrogradeTimestamp,
            ClockError::NegativeDelta => Self::NegativeDelta,
        }
    }
}

/// One event crossing the deterministic queue.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Event<'a> {
    /// Python-qualified record class name entering the dispatch identity.
    pub type_name: &'a str,
    /// Serialized event class.
    pub event_type: EventType,
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
    /// Event identifier.
    pub event_id: &'a str,
}

impl<'a> Event<'a> {
    /// Build an event with an explicit type name.
    pub const fn new(
        type_name: &'a str,
        event_type: EventType,
        timestamp_ns: i128,
        sequence: u64,
        event_id: &'a str,
    ) -> Self {
        Self {
            type_name,
            event_type,
            timestamp_ns,
            sequence,
            event_id,
        }
    }

    /// Build an event using the class's default Python-qualified type name.
    pub const fn new_default(
        event_type: EventType,
        timestamp_ns: i128,
        sequence: u64,
        event_id: &'a str,
    ) -> Self {
        Self::new(
            event_type.default_type_name(),
            event_type,
            timestamp_ns,
            sequence,
            event_id,
        )
    }
}

/// The ordering key of a dispatched event.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DispatchKey {
    /// Epoch nanoseconds.
    pub timestamp_ns: i128,
    /// Monotonic sequence number.
    pub sequence: u64,
    /// Event-class priority.
    pub class_priority: u8,
}

fn dispatch_key_less(a: DispatchKey, b: DispatchKey) -> bool {
    if a.timestamp_ns != b.timestamp_ns {
        return a.timestamp_ns < b.timestamp_ns;
    }
    if a.sequence != b.sequence {
        return a.sequence < b.sequence;
    }
    a.class_priority < b.class_priority
}

#[derive(Clone, Copy, Debug)]
struct QueuedEvent<'a> {
    timestamp_ns: i128,
    sequence: u64,
    class_priority: u8,
    feed_ordinal: u64,
    event: Event<'a>,
}

fn queued_less(a: QueuedEvent<'_>, b: QueuedEvent<'_>) -> bool {
    if a.timestamp_ns != b.timestamp_ns {
        return a.timestamp_ns < b.timestamp_ns;
    }
    if a.sequence != b.sequence {
        return a.sequence < b.sequence;
    }
    if a.class_priority != b.class_priority {
        return a.class_priority < b.class_priority;
    }
    a.feed_ordinal < b.feed_ordinal
}

fn heap_push<'a>(heap: &mut Vec<QueuedEvent<'a>>, item: QueuedEvent<'a>) {
    heap.push(item);
    let mut child = heap.len() - 1;
    while child > 0 {
        let parent = (child - 1) / 2;
        if !queued_less(heap[child], heap[parent]) {
            break;
        }
        heap.swap(child, parent);
        child = parent;
    }
}

fn sift_down<'a>(heap: &mut [QueuedEvent<'a>], root: usize) {
    let mut index = root;
    loop {
        let left = 2 * index + 1;
        let right = 2 * index + 2;
        let mut smallest = index;
        if left < heap.len() && queued_less(heap[left], heap[smallest]) {
            smallest = left;
        }
        if right < heap.len() && queued_less(heap[right], heap[smallest]) {
            smallest = right;
        }
        if smallest == index {
            return;
        }
        heap.swap(index, smallest);
        index = smallest;
    }
}

/// Priority queue ordered by time, source sequence, class, then feed order.
#[derive(Clone, Debug)]
pub struct EventQueue<'a> {
    clock: Option<SharedClock>,
    heap: Vec<QueuedEvent<'a>>,
    feed_ordinal: u64,
    last_dispatched_key: Option<DispatchKey>,
}

impl<'a> EventQueue<'a> {
    /// Create a queue with an optional shared clock.
    pub fn new(clock: Option<SharedClock>) -> Self {
        Self {
            clock,
            heap: Vec::new(),
            feed_ordinal: 0,
            last_dispatched_key: None,
        }
    }

    /// Number of queued events.
    pub fn count(&self) -> usize {
        self.heap.len()
    }

    /// Queue an event while preserving insertion order for exact ties.
    pub fn enqueue(&mut self, event: Event<'a>) -> Result<(), Error> {
        if event.event_id.is_empty() || event.type_name.is_empty() {
            return Err(Error::InvalidArgument);
        }
        let priority = event.event_type.class_priority();
        let key = DispatchKey {
            timestamp_ns: event.timestamp_ns,
            sequence: event.sequence,
            class_priority: priority,
        };
        let mut minimum: Option<i128> = self.last_dispatched_key.map(|last| last.timestamp_ns);
        if let Some(clock) = &self.clock {
            minimum = Some(clock.borrow().now());
        }
        if let Some(bound) = minimum {
            if event.timestamp_ns < bound {
                return Err(Error::StaleTimestamp);
            }
        }
        if let Some(last) = self.last_dispatched_key {
            if dispatch_key_less(key, last) {
                return Err(Error::RetrogradeOrderKey);
            }
        }
        heap_push(
            &mut self.heap,
            QueuedEvent {
                timestamp_ns: event.timestamp_ns,
                sequence: event.sequence,
                class_priority: priority,
                feed_ordinal: self.feed_ordinal,
                event,
            },
        );
        self.feed_ordinal += 1;
        Ok(())
    }

    /// Queue events in iteration order, which defines their feed order.
    pub fn enqueue_many(&mut self, events: &[Event<'a>]) -> Result<(), Error> {
        for &event in events {
            self.enqueue(event)?;
        }
        Ok(())
    }

    /// Return the next event without removing it.
    pub fn peek(&self) -> Option<Event<'a>> {
        self.heap.first().map(|queued| queued.event)
    }

    /// Return the next dispatchable event, or `None` if none is queued.
    pub fn pop(&mut self) -> Result<Option<Event<'a>>, Error> {
        if self.heap.is_empty() {
            return Ok(None);
        }
        let queued = self.heap[0];
        if let Some(clock) = &self.clock {
            clock
                .borrow_mut()
                .advance_to(queued.timestamp_ns)
                .map_err(Error::from)?;
        }
        let last = self.heap.pop().expect("heap is non-empty");
        if !self.heap.is_empty() {
            self.heap[0] = last;
            sift_down(&mut self.heap, 0);
        }
        self.last_dispatched_key = Some(DispatchKey {
            timestamp_ns: queued.timestamp_ns,
            sequence: queued.sequence,
            class_priority: queued.class_priority,
        });
        Ok(Some(queued.event))
    }
}

/// A synchronous event handler.
pub type Handler = for<'e> unsafe fn(*mut (), &Event<'e>);

/// Subscription selector.
#[derive(Clone, Copy, Debug)]
pub enum Selector<'a> {
    /// Match every event.
    Any,
    /// Match one event class.
    EventType(EventType),
    /// Match one Python-qualified type name.
    TypeName(&'a str),
}

impl Selector<'_> {
    fn matches(&self, event: &Event<'_>) -> bool {
        match self {
            Self::Any => true,
            Self::EventType(selected) => event.event_type == *selected,
            Self::TypeName(name) => event.type_name == *name,
        }
    }
}

#[derive(Clone, Copy)]
struct Subscription<'a> {
    selector: Selector<'a>,
    handler: Handler,
    context: *mut (),
}

/// Dispatch queued events synchronously in one deterministic event loop.
pub struct EventBus<'a> {
    queue: EventQueue<'a>,
    subscriptions: Vec<Subscription<'a>>,
    dispatch_digest: Sha256,
    dispatched_count: u64,
}

impl<'a> EventBus<'a> {
    /// Create a bus with an optional shared clock.
    pub fn new(clock: Option<SharedClock>) -> Self {
        Self {
            queue: EventQueue::new(clock),
            subscriptions: Vec::new(),
            dispatch_digest: Sha256::new(),
            dispatched_count: 0,
        }
    }

    /// Number of queued events.
    pub fn count(&self) -> usize {
        self.queue.count()
    }

    /// Number of dispatched events.
    pub fn dispatched_count(&self) -> u64 {
        self.dispatched_count
    }

    /// Return a stable lowercase hex SHA-256 digest of dispatched identities.
    pub fn dispatch_hash(&self) -> String {
        let digest = self.dispatch_digest.clone().finalize();
        let mut hex = String::with_capacity(64);
        const HEX: &[u8; 16] = b"0123456789abcdef";
        for byte in digest {
            hex.push(HEX[(byte >> 4) as usize] as char);
            hex.push(HEX[(byte & 0x0F) as usize] as char);
        }
        hex
    }

    /// Register a handler; handlers run in registration order.
    ///
    /// # Safety
    ///
    /// `context` is passed verbatim to `handler`. The caller must ensure the
    /// pointed-to value outlives the subscription, exactly like the Zig bus's
    /// `?*anyopaque` handler context.
    pub fn subscribe(
        &mut self,
        selector: Selector<'a>,
        handler: Handler,
        context: *mut (),
    ) -> Result<(), Error> {
        self.subscriptions.push(Subscription {
            selector,
            handler,
            context,
        });
        Ok(())
    }

    /// Publish one feed event to the deterministic queue.
    pub fn publish(&mut self, event: Event<'a>) -> Result<(), Error> {
        self.queue.enqueue(event)
    }

    /// Publish events in their incoming feed order.
    pub fn publish_many(&mut self, events: &[Event<'a>]) -> Result<(), Error> {
        self.queue.enqueue_many(events)
    }

    /// Schedule a venue response at its explicit event timestamp.
    pub fn schedule(&mut self, response: Event<'a>) -> Result<(), Error> {
        self.queue.enqueue(response)
    }

    /// Dispatch one event and return it, or return `None` if none is queued.
    ///
    /// Subscribers are snapshotted at dispatch start, so handlers that append
    /// subscriptions (or schedule events) during dispatch never receive the
    /// in-flight event, mirroring the Python bus.
    pub fn dispatch_next(&mut self) -> Result<Option<Event<'a>>, Error> {
        let Some(event) = self.queue.pop()? else {
            return Ok(None);
        };
        let registered = self.subscriptions.len();
        for index in 0..registered {
            let subscription = self.subscriptions[index];
            if subscription.selector.matches(&event) {
                // SAFETY: the handler and context were vetted by the caller at
                // subscription time, mirroring the Zig bus.
                unsafe { (subscription.handler)(subscription.context, &event) };
            }
        }
        self.record_dispatch(&event)?;
        Ok(Some(event))
    }

    /// Dispatch queued events until empty or `limit` is reached.
    pub fn drain(&mut self, limit: Option<usize>) -> Result<usize, Error> {
        let mut dispatched = 0usize;
        while limit.is_none_or(|limit| dispatched < limit) {
            let Some(_event) = self.dispatch_next()? else {
                break;
            };
            dispatched += 1;
        }
        Ok(dispatched)
    }

    fn record_dispatch(&mut self, event: &Event<'_>) -> Result<(), Error> {
        let mut buffer = [0u8; 1024];
        let mut writer = IdentityWriter::new(&mut buffer);
        writer.write_all(event.type_name.as_bytes())?;
        writer.write_byte(0)?;
        writer.write_all(event.event_type.value().as_bytes())?;
        writer.write_byte(0)?;
        let mut stamp_buffer = [0u8; 40];
        let stamp = isoformat_micros(event.timestamp_ns, &mut stamp_buffer)?;
        writer.write_all(&stamp_buffer[..stamp])?;
        writer.write_byte(0)?;
        writer.write_uint(event.sequence)?;
        writer.write_byte(0)?;
        writer.write_all(event.event_id.as_bytes())?;
        let length = writer.written();

        self.dispatch_digest.update(&(length as u64).to_be_bytes());
        self.dispatch_digest.update(&buffer[..length]);
        self.dispatched_count += 1;
        Ok(())
    }
}

struct IdentityWriter<'a> {
    buf: &'a mut [u8],
    index: usize,
}

impl<'a> IdentityWriter<'a> {
    fn new(buf: &'a mut [u8]) -> Self {
        Self { buf, index: 0 }
    }

    fn written(&self) -> usize {
        self.index
    }

    fn write_byte(&mut self, byte: u8) -> Result<(), Error> {
        if self.index >= self.buf.len() {
            return Err(Error::IdentityTooLong);
        }
        self.buf[self.index] = byte;
        self.index += 1;
        Ok(())
    }

    fn write_byte_n(&mut self, byte: u8, count: usize) -> Result<(), Error> {
        if self.index + count > self.buf.len() {
            return Err(Error::IdentityTooLong);
        }
        self.buf[self.index..self.index + count].fill(byte);
        self.index += count;
        Ok(())
    }

    fn write_all(&mut self, bytes: &[u8]) -> Result<(), Error> {
        if self.index + bytes.len() > self.buf.len() {
            return Err(Error::IdentityTooLong);
        }
        self.buf[self.index..self.index + bytes.len()].copy_from_slice(bytes);
        self.index += bytes.len();
        Ok(())
    }

    fn write_uint(&mut self, value: u64) -> Result<(), Error> {
        let mut digits = [0u8; 20];
        let mut length = digits.len();
        let mut remainder = value;
        loop {
            length -= 1;
            digits[length] = b'0' + (remainder % 10) as u8;
            remainder /= 10;
            if remainder == 0 {
                break;
            }
        }
        self.write_all(&digits[length..])
    }

    fn write_int_padded(&mut self, value: u64, width: usize) -> Result<(), Error> {
        let mut digits = [0u8; 20];
        let mut length = digits.len();
        let mut remainder = value;
        loop {
            length -= 1;
            digits[length] = b'0' + (remainder % 10) as u8;
            remainder /= 10;
            if remainder == 0 {
                break;
            }
        }
        let rendered = digits.len() - length;
        if rendered > width {
            return Err(Error::IdentityTooLong);
        }
        self.write_byte_n(b'0', width - rendered)?;
        self.write_all(&digits[length..])
    }
}

/// Render an epoch-nanosecond instant exactly like Python's
/// `datetime.isoformat(timespec="microseconds")` on a UTC-aware datetime.
fn isoformat_micros(timestamp_ns: i128, out: &mut [u8]) -> Result<usize, Error> {
    if timestamp_ns.rem_euclid(1_000) != 0 {
        return Err(Error::InvalidArgument);
    }
    let micros = timestamp_ns / 1_000;
    let micros_per_day: i128 = 86_400_000_000;
    let days = micros.div_euclid(micros_per_day);
    let micros_of_day = (micros - days * micros_per_day) as u64;
    let day_seconds = (micros_of_day / 1_000_000) as u32;
    let microsecond = (micros_of_day % 1_000_000) as u32;

    let civil = civil_from_days(days);
    if !(1..=9999).contains(&civil.year) {
        return Err(Error::InvalidArgument);
    }

    let mut writer = IdentityWriter::new(out);
    writer.write_int_padded(civil.year as u64, 4)?;
    writer.write_byte(b'-')?;
    writer.write_int_padded(civil.month, 2)?;
    writer.write_byte(b'-')?;
    writer.write_int_padded(civil.day, 2)?;
    writer.write_byte(b'T')?;
    writer.write_int_padded(u64::from(day_seconds / 3600), 2)?;
    writer.write_byte(b':')?;
    writer.write_int_padded(u64::from((day_seconds % 3600) / 60), 2)?;
    writer.write_byte(b':')?;
    writer.write_int_padded(u64::from(day_seconds % 60), 2)?;
    writer.write_byte(b'.')?;
    writer.write_int_padded(u64::from(microsecond), 6)?;
    writer.write_all(b"+00:00")?;
    Ok(writer.written())
}

struct Civil {
    year: i64,
    month: u64,
    day: u64,
}

/// Howard Hinnant's civil-from-days algorithm (same as `types.rs`).
fn civil_from_days(days_since_epoch: i128) -> Civil {
    let z = days_since_epoch + 719_468;
    let era = z.div_euclid(146_097);
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = i128::from(yoe) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    Civil {
        year: (year + if month <= 2 { 1 } else { 0 }) as i64,
        month,
        day,
    }
}
