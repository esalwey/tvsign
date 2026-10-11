//! Wireless RSD access to a device we already paired with.
//!
//! The whole chain runs in userspace, with no TUN interface or admin rights:
//! mDNS `_remotepairing._tcp` -> pair-verify with the stored `RpPairingFile` ->
//! `createListener` -> TLS-PSK tunnel -> jktcp TCP stack -> RSD handshake.

use std::net::SocketAddr;
use std::time::Duration;

use idevice::IdeviceError;
use idevice::remote_pairing::{
    RemotePairingClient, RpPairingFile, RpPairingSocket, connect_tls_psk_tunnel_native,
};
use idevice::rsd::RsdHandshake;
use idevice::tcp::adapter::Adapter;
use idevice::tcp::handle::AdapterHandle;
use mdns_sd::{ServiceDaemon, ServiceEvent};

use super::discovery::{DiscoveredDevice, ServiceKind};
use super::store::{PairingRecord, PairingStore};
use crate::Error;

/// How long a single network step may take before the device counts as unreachable.
const STEP_TIMEOUT: Duration = Duration::from_secs(15);

/// A paired device seen on the network whose `authTag` proves it is ours.
#[derive(Debug, Clone)]
pub struct LocatedDevice {
    pub record: PairingRecord,
    /// `identifier` TXT record of the device's `_remotepairing._tcp` service.
    pub service_identifier: String,
    pub address: SocketAddr,
}

/// An open tunnel to a device, ready for RSD services.
pub struct RsdConnection {
    pub device: LocatedDevice,
    pub adapter: AdapterHandle,
    pub handshake: RsdHandshake,
}

/// Finds a paired device by stored UDID or by its mDNS `identifier`.
///
/// Only `_remotepairing._tcp` is browsed; the manual pairing service, which
/// shows a PIN on screen when connected to, is never looked at. A device is
/// only returned once its `authTag` validates against one of our pairings, so
/// devices paired with other hosts are never targeted.
pub async fn locate(
    store: &PairingStore,
    identifier: &str,
    timeout: Duration,
) -> Result<LocatedDevice, Error> {
    let records = store.list().await?;
    if records.is_empty() {
        return Err(Error::PairingNotFound(identifier.to_string()));
    }

    let wanted_udid = records
        .iter()
        .find(|record| record.udid.eq_ignore_ascii_case(identifier))
        .map(|record| record.udid.clone());
    let wanted_name = records
        .iter()
        .find(|record| record.udid.eq_ignore_ascii_case(identifier))
        .map(|record| record.name.clone());
    // A device with our device's name whose tag none of our keys produce: probably our device,
    // which forgot this host. Only reported if the real one never shows up before the deadline.
    let mut seen_forgotten = false;

    let daemon = ServiceDaemon::new()?;
    let browse = daemon.browse(ServiceKind::RemotePairing.service_type())?;
    let deadline = tokio::time::Instant::now() + timeout;
    let mut result = Err(Error::DeviceNotFound(identifier.to_string()));

    loop {
        let event = tokio::select! {
            _ = tokio::time::sleep_until(deadline) => break,
            event = browse.recv_async() => event,
        };
        let Ok(event) = event else { break };

        let ServiceEvent::ServiceResolved(service) = event else {
            continue;
        };
        let Some(device) = DiscoveredDevice::from_resolved(ServiceKind::RemotePairing, &service)
        else {
            continue;
        };
        let (Some(service_identifier), Some(auth_tag)) = (&device.identifier, &device.auth_tag)
        else {
            continue;
        };

        let mentioned = service_identifier.eq_ignore_ascii_case(identifier);

        match store.find(service_identifier, auth_tag).await? {
            Some(record)
                if mentioned
                    || wanted_udid
                        .as_deref()
                        .is_some_and(|udid| udid.eq_ignore_ascii_case(&record.udid)) =>
            {
                log::debug!(
                    "{} is {} at {}",
                    service_identifier,
                    record.udid,
                    device.address
                );
                result = Ok(LocatedDevice {
                    record,
                    service_identifier: service_identifier.clone(),
                    address: device.address,
                });
                break;
            }
            // The device we were asked for, but none of our keys produce its
            // tag: it was reset or forgot this host.
            None if mentioned => {
                result = Err(Error::PairingNotAccepted);
                break;
            }
            None => {
                if let (Some(wanted), Some(name)) = (&wanted_name, &device.name) {
                    seen_forgotten |= wanted.eq_ignore_ascii_case(name);
                }
            }
            _ => {}
        }
    }

    if seen_forgotten && matches!(result, Err(Error::DeviceNotFound(_))) {
        result = Err(Error::PairingNotAccepted);
    }

    if let Err(e) = daemon.shutdown() {
        log::debug!("failed to shut down mDNS daemon: {e}");
    }

    result
}

/// Pair-verifies with `device` and opens a tunnel to its RSD service.
///
/// Never falls back to pairing: if the device no longer accepts our keys this
/// returns [`Error::PairingNotAccepted`] without a PIN being shown on screen.
pub async fn connect_rsd(
    store: &PairingStore,
    device: LocatedDevice,
    host: &str,
) -> Result<RsdConnection, Error> {
    let mut pairing_file = store.load(&device.record.udid).await?;
    let (adapter, handshake) = open_tunnel(device.address, host, &mut pairing_file).await?;

    Ok(RsdConnection {
        device,
        adapter,
        handshake,
    })
}

/// [`locate`] then [`connect_rsd`].
pub async fn locate_and_connect(
    store: &PairingStore,
    identifier: &str,
    host: &str,
    timeout: Duration,
) -> Result<RsdConnection, Error> {
    let device = locate(store, identifier, timeout).await?;
    connect_rsd(store, device, host).await
}

async fn open_tunnel(
    address: SocketAddr,
    host: &str,
    pairing_file: &mut RpPairingFile,
) -> Result<(AdapterHandle, RsdHandshake), Error> {
    let stream = step(tokio::net::TcpStream::connect(address))
        .await?
        .map_err(Error::Unreachable)?;
    let mut client = RemotePairingClient::new(RpPairingSocket::new(stream), host, pairing_file);

    step(client.attempt_pair_verify()).await??;
    if let Err(e) = step(client.validate_pairing()).await? {
        return Err(match e {
            IdeviceError::Socket(io) => Error::Unreachable(io),
            e => {
                log::debug!("pair-verify rejected: {e}");
                Error::PairingNotAccepted
            }
        });
    }

    let tunnel_port = step(client.create_tcp_listener()).await??;
    let tunnel_stream = step(tokio::net::TcpStream::connect(SocketAddr::new(
        address.ip(),
        tunnel_port,
    )))
    .await?
    .map_err(Error::Unreachable)?;
    let tunnel = step(connect_tls_psk_tunnel_native(
        Box::new(tunnel_stream),
        client.encryption_key(),
    ))
    .await??;

    let client_ip = tunnel
        .info
        .client_address
        .parse()
        .map_err(|e| Error::Other(format!("bad tunnel client address: {e}")))?;
    let server_ip = tunnel
        .info
        .server_address
        .parse()
        .map_err(|e| Error::Other(format!("bad tunnel server address: {e}")))?;
    let mtu = tunnel.info.mtu as usize;
    let rsd_port = tunnel.info.server_rsd_port;

    let mut adapter = Adapter::new(Box::new(tunnel.into_inner()), client_ip, server_ip);
    adapter.set_mss(mtu.saturating_sub(60));
    let mut adapter = adapter.to_async_handle();

    let rsd_stream = step(adapter.connect(rsd_port))
        .await?
        .map_err(Error::Unreachable)?;
    let handshake = step(RsdHandshake::new(rsd_stream)).await??;

    Ok((adapter, handshake))
}

async fn step<F: std::future::Future>(future: F) -> Result<F::Output, Error> {
    tokio::time::timeout(STEP_TIMEOUT, future)
        .await
        .map_err(|_| {
            Error::Unreachable(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "device stopped responding",
            ))
        })
}
