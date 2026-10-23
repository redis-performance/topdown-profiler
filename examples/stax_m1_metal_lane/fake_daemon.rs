use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

use staxd_proto::{
    DaemonStatus, KdBufBatch, RecordError, RecordSummary, SessionConfig, SessionState, Staxd,
    StaxdDispatcher,
};

#[derive(Clone, Default)]
struct Fake {
    active: Arc<AtomicBool>,
}

impl Staxd for Fake {
    async fn status(&self) -> DaemonStatus {
        DaemonStatus {
            version: "fake-no-kperf".to_owned(),
            state: SessionState::Idle,
            host_arch: "aarch64".to_owned(),
        }
    }

    async fn record(
        &self,
        _config: SessionConfig,
        records: vox::Tx<KdBufBatch>,
    ) -> Result<RecordSummary, RecordError> {
        self.active.store(true, Ordering::Release);
        let started = Instant::now();
        while self.active.load(Ordering::Acquire) {
            let batch = KdBufBatch {
                records: Vec::new(),
                read_started_mach_ticks: 0,
                drained_mach_ticks: 0,
                queued_for_send_mach_ticks: 0,
                send_started_mach_ticks: 0,
                drained_at_unix_ns: 0,
            };
            if records.send(batch).await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        self.active.store(false, Ordering::Release);
        Ok(RecordSummary {
            records_drained: 0,
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
