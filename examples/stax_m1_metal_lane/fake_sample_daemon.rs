//! Rootless software-path fixture. Every PET and PMC value below is invented.

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

use mach2::mach_time::mach_absolute_time;
use stax_mac_kperf_sys::kdebug::{
    DBG_FUNC_END, DBG_FUNC_NONE, DBG_FUNC_START, DBG_PERF, KdBuf, kdbg_eventid, perf,
};
use staxd_proto::{
    DaemonStatus, KdBufBatch, RecordError, RecordSummary, SessionConfig, SessionState, Staxd,
    StaxdDispatcher,
};

#[derive(Clone, Default)]
struct Fake {
    active: Arc<AtomicBool>,
}

fn record(timestamp: u64, debugid: u32, args: [u64; 4]) -> KdBuf {
    KdBuf {
        timestamp,
        arg1: args[0],
        arg2: args[1],
        arg3: args[2],
        arg4: args[3],
        arg5: 42,
        debugid,
        cpuid: 0,
        unused: 0,
    }
}

fn fixture_sample(timestamp: u64) -> Vec<KdBuf> {
    let id = |subclass, code, function| kdbg_eventid(DBG_PERF, subclass, code) | function;
    vec![
        record(timestamp, id(perf::sc::GENERIC, 0, DBG_FUNC_START), [0; 4]),
        record(
            timestamp,
            id(perf::sc::CALLSTACK, perf::cs::UHDR, DBG_FUNC_NONE),
            [0, 1, 0, 0],
        ),
        record(
            timestamp,
            id(perf::sc::CALLSTACK, perf::cs::UDATA, DBG_FUNC_NONE),
            [0x1000_0000_0, 0, 0, 0],
        ),
        record(
            timestamp,
            id(perf::sc::KPC, perf::kpc::DATA_THREAD, DBG_FUNC_NONE),
            [100, 200, 13, 0],
        ),
        record(
            timestamp,
            id(perf::sc::KPC, perf::kpc::DATA_THREAD, DBG_FUNC_NONE),
            [0, 17, 0, 0],
        ),
        record(timestamp, id(perf::sc::GENERIC, 0, DBG_FUNC_END), [0; 4]),
    ]
}

impl Staxd for Fake {
    async fn status(&self) -> DaemonStatus {
        DaemonStatus {
            version: "synthetic-pet-pmc-fixture".to_owned(),
            state: SessionState::Idle,
            host_arch: "aarch64".to_owned(),
        }
    }

    async fn record(
        &self,
        config: SessionConfig,
        records: vox::Tx<KdBufBatch>,
    ) -> Result<RecordSummary, RecordError> {
        if config.class_mask != 3 || config.pmu_event_configs != [163, 0, 0, 203, 0, 0, 0, 0] {
            return Err(RecordError::Kperf {
                op: "synthetic fixture expected M1 PMU configuration".to_owned(),
                code: -1,
            });
        }
        self.active.store(true, Ordering::Release);
        let started = Instant::now();
        let mut drained = 0;
        while self.active.load(Ordering::Acquire) {
            let timestamp = unsafe { mach_absolute_time() };
            let fixture = fixture_sample(timestamp);
            drained += fixture.len() as u64;
            let batch = KdBufBatch {
                records: fixture,
                read_started_mach_ticks: timestamp,
                drained_mach_ticks: timestamp,
                queued_for_send_mach_ticks: timestamp,
                send_started_mach_ticks: timestamp,
                drained_at_unix_ns: 0,
            };
            if records.send(batch).await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        self.active.store(false, Ordering::Release);
        Ok(RecordSummary {
            records_drained: drained,
            session_ns: started.elapsed().as_nanos() as u64,
        })
    }

    async fn stop_recording(&self) -> bool {
        self.active.swap(false, Ordering::AcqRel)
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let socket = std::env::args()
        .nth(1)
        .expect("private socket path required");
    let listener = vox::transport::local::LocalLinkAcceptor::bind(socket)?;
    let fake = Fake::default();
    loop {
        let link = listener.accept().await?;
        let dispatcher = StaxdDispatcher::new(fake.clone());
        tokio::spawn(async move {
            if let Ok(connection) = vox::acceptor_on(link)
                .on_lane(dispatcher)
                .establish_connection()
                .await
            {
                connection.closed().await;
            }
        });
    }
}
