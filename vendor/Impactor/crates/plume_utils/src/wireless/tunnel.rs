//! Wireless RSD access to a device we already paired with.
//!
//! The whole chain runs in userspace, with no TUN interface or admin rights:
//! mDNS `_remotepairing._tcp` -> pair-verify with the stored `RpPairingFile` ->
//! `createListener` -> TLS-PSK tunnel -> jktcp TCP stack -> RSD handshake.

use std::net::SocketAddr;
use std::time::Duration;

use idevice::IdeviceError;
use idevice::remote_pairing::errors::RemotePairingError;
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
    // Un appareil du meme nom dont le tag ne correspond a aucune de nos cles : peut etre un autre
    // appareil (le nom est librement modifiable, « Apple TV » par defaut). Simple indice dans le
    // message, jamais une preuve que l'appairage est perdu.
    let mut same_name_seen: Option<String> = None;

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
                    if wanted.eq_ignore_ascii_case(name) {
                        same_name_seen = Some(name.clone());
                    }
                }
            }
            _ => {}
        }
    }

    if let Some(name) = same_name_seen {
        if matches!(result, Err(Error::DeviceNotFound(_))) {
            log::warn!(
                "a device named {name:?} is on the network but is not paired with this host; \
                 not treating the pairing as lost (it may be another device with the same name)"
            );
            result = Err(Error::DeviceNotFound(format!(
                "{identifier} (a different device named {name:?} is visible but not paired with this host)"
            )));
        }
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
        return Err(classify_verify_error(e));
    }

    let tunnel_port = step(client.create_tcp_listener()).await??;
    let tunnel_stream = step(tokio::net::TcpStream::connect(tunnel_address(
        address,
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

/// Adresse du tunnel : celle de l'appareil avec un autre port. On part de `address` elle-meme pour que
/// l'identifiant de portee d'une adresse IPv6 link-local (fe80::...) ne soit pas perdu.
fn tunnel_address(address: SocketAddr, port: u16) -> SocketAddr {
    let mut tunnel = address;
    tunnel.set_port(port);
    tunnel
}

/// Seul un refus explicite de l'appareil prouve que l'appairage est perdu. Une reponse inattendue
/// (appareil qui se reveille, TLV tronque...) est une panne passagere, a reessayer plus tard.
fn classify_verify_error(error: IdeviceError) -> Error {
    match error {
        IdeviceError::Socket(io) => Error::Unreachable(io),
        IdeviceError::RemotePairing(
            RemotePairingError::PairVerifyFailed | RemotePairingError::PairingRejected(_),
        ) => {
            log::debug!("pair-verify rejected by the device");
            Error::PairingNotAccepted
        }
        other => {
            log::debug!("pair-verify failed without a clear rejection: {other}");
            Error::Unreachable(std::io::Error::new(
                std::io::ErrorKind::Other,
                format!("pair-verify answer not understood: {other}"),
            ))
        }
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Ipv6Addr, SocketAddrV6};

    #[test]
    fn tunnel_address_keeps_the_ipv6_scope_id() {
        let ll = SocketAddr::V6(SocketAddrV6::new(
            Ipv6Addr::new(0xfe80, 0, 0, 0, 1, 2, 3, 4),
            49152,
            0,
            7,
        ));
        let tunnel = tunnel_address(ll, 50000);
        match tunnel {
            SocketAddr::V6(v6) => {
                assert_eq!(v6.scope_id(), 7);
                assert_eq!(v6.port(), 50000);
            }
            _ => panic!("expected an IPv6 address"),
        }
    }

    #[test]
    fn tunnel_address_replaces_the_port_for_ipv4() {
        let v4: SocketAddr = "192.168.1.20:49152".parse().unwrap();
        assert_eq!(tunnel_address(v4, 1234).to_string(), "192.168.1.20:1234");
    }

    #[test]
    fn only_an_explicit_rejection_means_pairing_lost() {
        assert!(matches!(
            classify_verify_error(RemotePairingError::PairVerifyFailed.into()),
            Error::PairingNotAccepted
        ));
        assert!(matches!(
            classify_verify_error(RemotePairingError::PairingRejected("no".into()).into()),
            Error::PairingNotAccepted
        ));
    }

    #[test]
    fn unexpected_answers_are_retryable_not_pairing_lost() {
        assert!(matches!(
            classify_verify_error(IdeviceError::UnexpectedResponse("x".into())),
            Error::Unreachable(_)
        ));
        assert!(matches!(
            classify_verify_error(IdeviceError::NotEnoughBytes(32, 3)),
            Error::Unreachable(_)
        ));
        let io = std::io::Error::new(std::io::ErrorKind::ConnectionReset, "reset");
        assert!(matches!(
            classify_verify_error(IdeviceError::Socket(io)),
            Error::Unreachable(_)
        ));
    }
}
