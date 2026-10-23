//! Owns bounded worker reservations, attachment, renewal and release.
//! Hosts supply conditional persistence; no process or transport implementation.
use crate::{
    persistence::{error, Result},
    store::{decode, encode, ExecutionStore},
};
use chrono::Utc;
use serde::{Deserialize, Serialize};
#[derive(Serialize, Deserialize)]
struct Fleet {
    owners: Vec<PoolOwner>,
}
#[derive(Serialize, Deserialize)]
struct PoolOwner {
    token: String,
    expires: chrono::DateTime<Utc>,
    #[serde(deserialize_with = "serde::Deserialize::deserialize")]
    worker: Option<String>,
}
pub struct Slot {
    store: ExecutionStore,
    token: String,
    worker: String,
    attached_until: chrono::DateTime<Utc>,
}
impl Slot {
    /// Reserve before starting compute, including its cold boot interval.
    pub fn reserve(store: &ExecutionStore, max: usize) -> Result<Option<String>> {
        if max == 0 || max > 16 {
            return Err(error("execution fleet size must be 1..16"));
        }
        store.backend.create(
            "execution/control/fleet.json",
            &encode(&Fleet { owners: Vec::new() })?,
        )?;
        let token = uuid::Uuid::new_v4().to_string();
        let mut claimed = false;
        store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                claimed = false;
                let mut fleet: Fleet = decode(&bytes)?;
                fleet.owners.retain(|o| o.expires > Utc::now());
                if fleet.owners.iter().any(|owner| owner.token == token) {
                    // A committed PUT may lose its acknowledgement; an SDK
                    // retry then reports a conflict against that same write.
                    claimed = true;
                } else if fleet.owners.len() < max {
                    fleet.owners.push(PoolOwner {
                        token: token.clone(),
                        expires: Utc::now() + chrono::Duration::seconds(600),
                        worker: None,
                    });
                    claimed = true;
                }
                encode(&fleet)
            })?;
        Ok(claimed.then_some(token))
    }
    pub fn attach(store: &ExecutionStore, token: String) -> Result<Self> {
        let worker = uuid::Uuid::new_v4().to_string();
        let mut attached_until = Utc::now();
        store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                let owner = fleet
                    .owners
                    .iter_mut()
                    .find(|owner| {
                        owner.token == token
                            && owner.expires > Utc::now()
                            && (owner.worker.is_none() || owner.worker.as_deref() == Some(&worker))
                    })
                    .ok_or_else(|| {
                        crate::jobs::StoreError::OwnershipLost(
                            "worker reservation expired or was already consumed".into(),
                        )
                    })?;
                if owner.worker.is_none() {
                    owner.worker = Some(worker.clone());
                    owner.expires = Utc::now() + chrono::Duration::seconds(60);
                }
                attached_until = owner.expires;
                encode(&fleet)
            })?;
        Ok(Self {
            store: store.clone(),
            token,
            worker,
            attached_until,
        })
    }
    pub fn acquire(store: &ExecutionStore, max: usize) -> Result<Option<Self>> {
        Self::reserve(store, max)?
            .map(|token| Self::attach(store, token))
            .transpose()
    }
    pub fn release_unstarted(store: &ExecutionStore, token: &str) -> Result<()> {
        store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                fleet
                    .owners
                    .retain(|owner| owner.token != token || owner.worker.is_some());
                encode(&fleet)
            })
    }
    pub fn attached_until(&self) -> chrono::DateTime<Utc> {
        self.attached_until
    }
    pub fn renew(&self) -> Result<chrono::DateTime<Utc>> {
        let mut expires = Utc::now();
        self.store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                let owner = fleet
                    .owners
                    .iter_mut()
                    .find(|o| {
                        o.token == self.token
                            && o.worker.as_deref() == Some(&self.worker)
                            && o.expires > Utc::now()
                    })
                    .ok_or_else(|| {
                        crate::jobs::StoreError::OwnershipLost(
                            "execution fleet slot expired or replaced".into(),
                        )
                    })?;
                owner.expires = Utc::now() + chrono::Duration::seconds(60);
                expires = owner.expires;
                encode(&fleet)
            })
            .map(|_| expires)
    }
}
impl Drop for Slot {
    fn drop(&mut self) {
        let _ = self
            .store
            .backend
            .update("execution/control/fleet.json", &mut |bytes| {
                let mut fleet: Fleet = decode(&bytes)?;
                fleet
                    .owners
                    .retain(|o| o.token != self.token || o.worker.as_deref() != Some(&self.worker));
                encode(&fleet)
            });
    }
}
