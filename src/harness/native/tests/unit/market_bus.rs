use super::{shared_clock, Event, EventBus, EventQueue, EventType, Selector};

const STAMP_NS: i128 = 1_705_674_600_000_000_000;
const MICRO: i128 = 1_000;

#[test]
fn event_class_priority_mirrors_the_python_table_exactly() {
    let expected = [
        (EventType::Quote, 0u8),
        (EventType::Trade, 1),
        (EventType::Order, 2),
        (EventType::MultiLegOrder, 3),
        (EventType::ExecutionReport, 4),
        (EventType::Fill, 5),
        (EventType::Position, 6),
        (EventType::Book, 7),
        (EventType::Account, 8),
    ];
    for (event_type, priority) in expected {
        assert_eq!(event_type.class_priority(), priority);
    }
}

#[test]
fn isoformat_microseconds_matches_python_datetime() {
    let mut buffer = [0u8; 40];
    let whole = super::isoformat_micros(STAMP_NS, &mut buffer).unwrap();
    assert_eq!(&buffer[..whole], b"2024-01-19T14:30:00.000000+00:00");
    let offset = super::isoformat_micros(STAMP_NS + 29 * MICRO, &mut buffer).unwrap();
    assert_eq!(&buffer[..offset], b"2024-01-19T14:30:00.000029+00:00");
    assert!(super::isoformat_micros(STAMP_NS + 1, &mut buffer).is_err());
}

#[test]
fn queue_orders_by_timestamp_sequence_class_then_feed_order() {
    let mut queue = EventQueue::new(Some(shared_clock(STAMP_NS)));
    let events = [
        Event::new_default(EventType::Fill, STAMP_NS + MICRO, 4, "fill-priority"),
        Event::new_default(EventType::Trade, STAMP_NS + MICRO, 4, "trade-priority"),
        Event::new_default(EventType::Quote, STAMP_NS + MICRO, 4, "quote-first"),
        Event::new_default(EventType::Quote, STAMP_NS + MICRO, 4, "quote-second"),
        Event::new_default(EventType::Quote, STAMP_NS + MICRO, 5, "later-sequence"),
        Event::new_default(EventType::Fill, STAMP_NS + MICRO, 3, "earlier-sequence"),
        Event::new_default(EventType::Trade, STAMP_NS, 100, "earliest-time"),
    ];
    queue.enqueue_many(&events).unwrap();
    assert_eq!(queue.count(), 7);

    let expected = [
        "earliest-time",
        "earlier-sequence",
        "quote-first",
        "quote-second",
        "trade-priority",
        "fill-priority",
        "later-sequence",
    ];
    let mut dispatched = 0usize;
    while let Some(event) = queue.pop().unwrap() {
        assert_eq!(expected[dispatched], event.event_id);
        dispatched += 1;
    }
    assert_eq!(dispatched, expected.len());
    assert!(queue.peek().is_none());
}

#[test]
fn queue_rejects_events_older_than_the_sim_clock_head() {
    let sim = shared_clock(STAMP_NS);
    let mut queue = EventQueue::new(Some(sim.clone()));

    assert_eq!(
        queue.enqueue(Event::new_default(
            EventType::Quote,
            STAMP_NS - MICRO,
            0,
            "stale"
        )),
        Err(super::Error::StaleTimestamp)
    );

    queue
        .enqueue(Event::new_default(EventType::Quote, STAMP_NS, 0, "current"))
        .unwrap();
    assert!(queue.pop().unwrap().is_some());
    assert_eq!(sim.borrow().now(), STAMP_NS);

    sim.borrow_mut().advance_by(2 * MICRO).unwrap();
    assert_eq!(
        queue.enqueue(Event::new_default(
            EventType::Quote,
            STAMP_NS + MICRO,
            1,
            "now-stale"
        )),
        Err(super::Error::StaleTimestamp)
    );
}

struct VenueSink {
    bus: *mut EventBus<'static>,
    handled: Vec<String>,
}

unsafe fn on_quote(context: *mut (), event: &Event<'_>) {
    let sink = unsafe { &mut *(context as *mut VenueSink) };
    sink.handled.push(event.event_id.to_owned());
    let bus = unsafe { &mut *sink.bus };
    let _ = bus.schedule(Event::new_default(
        EventType::Fill,
        event.timestamp_ns + 250 * MICRO,
        event.sequence,
        "venue-fill",
    ));
}

unsafe fn on_fill(context: *mut (), event: &Event<'_>) {
    let sink = unsafe { &mut *(context as *mut VenueSink) };
    sink.handled.push(event.event_id.to_owned());
}

#[test]
fn handler_can_schedule_a_future_venue_response() {
    let sim = shared_clock(STAMP_NS);
    let mut bus = EventBus::new(Some(sim.clone()));
    let mut sink = VenueSink {
        bus: &mut bus,
        handled: Vec::new(),
    };
    let sink_ptr = &mut sink as *mut VenueSink as *mut ();
    bus.subscribe(Selector::EventType(EventType::Quote), on_quote, sink_ptr)
        .unwrap();
    bus.subscribe(Selector::EventType(EventType::Fill), on_fill, sink_ptr)
        .unwrap();
    bus.publish(Event::new_default(
        EventType::Quote,
        STAMP_NS,
        7,
        "market-quote",
    ))
    .unwrap();

    assert_eq!(bus.drain(None).unwrap(), 2);
    assert_eq!(bus.dispatched_count(), 2);
    assert_eq!(sink.handled[0], "market-quote");
    assert_eq!(sink.handled[1], "venue-fill");
    assert_eq!(sim.borrow().now(), STAMP_NS + 250 * MICRO);
}

#[test]
fn scheduled_response_cannot_precede_the_last_dispatch_key() {
    let sim = shared_clock(STAMP_NS);
    let mut bus = EventBus::new(Some(sim));
    bus.publish(Event::new_default(
        EventType::Trade,
        STAMP_NS,
        7,
        "market-trade",
    ))
    .unwrap();
    assert!(bus.dispatch_next().unwrap().is_some());

    assert_eq!(
        bus.schedule(Event::new_default(
            EventType::Quote,
            STAMP_NS,
            7,
            "retrograde-response"
        )),
        Err(super::Error::RetrogradeOrderKey)
    );
}

#[test]
fn dispatch_digest_is_byte_identical_to_the_python_bus() {
    let sim = shared_clock(STAMP_NS);
    let mut bus = EventBus::new(Some(sim));
    let ids: Vec<String> = (0..10_000usize)
        .map(|index| format!("event-{index}"))
        .collect();
    for (index, id) in ids.iter().enumerate() {
        let offset_us = (index % 29) as i128;
        let sequence = (index % 17) as u64;
        let event = match index % 3 {
            0 => Event::new_default(
                EventType::Quote,
                STAMP_NS + offset_us * MICRO,
                sequence,
                id.as_str(),
            ),
            1 => Event::new_default(
                EventType::Trade,
                STAMP_NS + offset_us * MICRO,
                sequence,
                id.as_str(),
            ),
            _ => Event::new_default(
                EventType::Fill,
                STAMP_NS + offset_us * MICRO,
                sequence,
                id.as_str(),
            ),
        };
        bus.publish(event).unwrap();
    }

    assert_eq!(bus.drain(None).unwrap(), 10_000);
    assert_eq!(bus.dispatched_count(), 10_000);
    assert_eq!(
        bus.dispatch_hash(),
        "949b0dd3fbf95bcb1b93baec06c20e8e5c66ad74e342df01b1e21d9eff769df2"
    );
}

struct SchedulerSink {
    bus: *mut EventBus<'static>,
    seen: Vec<String>,
    timestamps: Vec<i128>,
    recurring_remaining: usize,
}

unsafe fn on_scheduled(context: *mut (), event: &Event<'_>) {
    let sink = unsafe { &mut *(context as *mut SchedulerSink) };
    sink.seen.push(event.event_id.to_owned());
    sink.timestamps.push(event.timestamp_ns);
    if event.event_id.starts_with("callback-recurring") && sink.recurring_remaining > 0 {
        sink.recurring_remaining -= 1;
        // SAFETY: recurring callback ids are `'static` string literals in
        // this test, mirroring the Zig handler that reuses the borrowed id.
        let event_id = unsafe { core::mem::transmute::<&str, &'static str>(event.event_id) };
        let bus = unsafe { &mut *sink.bus };
        let _ = bus.schedule(Event::new(
            "harness.core.scheduler.ScheduledCallbackEvent",
            EventType::Order,
            event.timestamp_ns + 1_000_000_000,
            event.sequence + 10,
            event_id,
        ));
    }
}

#[test]
fn absolute_and_recurring_callbacks_are_synchronous_and_deterministic() {
    let sim = shared_clock(STAMP_NS);
    let mut bus = EventBus::new(Some(sim.clone()));
    let mut sink = SchedulerSink {
        bus: &mut bus,
        seen: Vec::new(),
        timestamps: Vec::new(),
        recurring_remaining: 2,
    };
    let sink_ptr = &mut sink as *mut SchedulerSink as *mut ();
    bus.subscribe(
        Selector::TypeName("harness.core.scheduler.ScheduledCallbackEvent"),
        on_scheduled,
        sink_ptr,
    )
    .unwrap();

    bus.schedule(Event::new(
        "harness.core.scheduler.ScheduledCallbackEvent",
        EventType::Order,
        STAMP_NS + 2 * 1_000_000_000,
        1,
        "callback-absolute",
    ))
    .unwrap();
    bus.schedule(Event::new(
        "harness.core.scheduler.ScheduledCallbackEvent",
        EventType::Order,
        STAMP_NS + 1_000_000_000,
        0,
        "callback-recurring",
    ))
    .unwrap();

    bus.drain(None).unwrap();

    let expected_ids = [
        "callback-recurring",
        "callback-absolute",
        "callback-recurring",
        "callback-recurring",
    ];
    assert_eq!(sink.seen.len(), expected_ids.len());
    for (expected, actual) in expected_ids.iter().zip(sink.seen.iter()) {
        assert_eq!(expected, actual);
    }
    assert_eq!(sink.timestamps[0], STAMP_NS + 1_000_000_000);
    assert_eq!(sink.timestamps[1], STAMP_NS + 2 * 1_000_000_000);
    assert_eq!(sink.timestamps[2], STAMP_NS + 2 * 1_000_000_000);
    assert_eq!(sink.timestamps[3], STAMP_NS + 3 * 1_000_000_000);
    assert_eq!(sim.borrow().now(), STAMP_NS + 3 * 1_000_000_000);
}

#[test]
fn queue_rejects_events_without_identity() {
    let mut queue = EventQueue::new(Some(shared_clock(STAMP_NS)));
    assert_eq!(
        queue.enqueue(Event::new_default(EventType::Quote, STAMP_NS, 0, "")),
        Err(super::Error::InvalidArgument)
    );
}
