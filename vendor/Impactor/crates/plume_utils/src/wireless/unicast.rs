//! Direct ("unicast") mDNS queries to one known address.
//!
//! Multicast discovery (`discovery::discover`, `tunnel::locate`) needs the mDNS group
//! 224.0.0.251 to reach us: it does not on an IoT VLAN, from a Docker bridge network, behind
//! a multicast-filtering access point or through a strict firewall profile. mDNS responders
//! answer a plain DNS query sent straight to UDP port 5353 (RFC 6762 section 5.5, "legacy
//! unicast"), which routes like any other UDP packet. This module asks one address for its
//! `_remotepairing._tcp` / `_remotepairing-manual-pairing._tcp` service and builds the same
//! [`DiscoveredDevice`] the multicast browse would have produced.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

use tokio::net::UdpSocket;

use super::discovery::{DiscoveredDevice, ServiceKind, trim_local};
use crate::Error;

/// The standard mDNS port.
pub const MDNS_PORT: u16 = 5353;

/// How long a direct query waits for the answer.
pub const DIRECT_TIMEOUT: Duration = Duration::from_secs(2);

const TYPE_A: u16 = 1;
const TYPE_PTR: u16 = 12;
const TYPE_TXT: u16 = 16;
const TYPE_AAAA: u16 = 28;
const TYPE_SRV: u16 = 33;
const CLASS_IN: u16 = 1;

/// Where to send the direct queries: the device's address and its mDNS port.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Hint {
    pub addr: SocketAddr,
}

impl Hint {
    /// The standard mDNS port on `ip`.
    pub fn new(ip: IpAddr) -> Self {
        Self::with_port(ip, MDNS_PORT)
    }

    pub fn with_port(ip: IpAddr, port: u16) -> Self {
        Self {
            addr: SocketAddr::new(ip, port),
        }
    }
}

// ---------- DNS wire format ----------

fn encode_name(name: &str, out: &mut Vec<u8>) {
    for label in name.trim_end_matches('.').split('.').filter(|l| !l.is_empty()) {
        let bytes = label.as_bytes();
        let len = bytes.len().min(63);
        out.push(len as u8);
        out.extend_from_slice(&bytes[..len]);
    }
    out.push(0);
}

/// A standard query (recursion not desired) for `questions`, as `(name, type)` pairs.
fn encode_query(id: u16, questions: &[(&str, u16)]) -> Vec<u8> {
    let mut out = Vec::with_capacity(64);
    out.extend_from_slice(&id.to_be_bytes());
    out.extend_from_slice(&0u16.to_be_bytes()); // flags: query
    out.extend_from_slice(&(questions.len() as u16).to_be_bytes());
    out.extend_from_slice(&[0, 0, 0, 0, 0, 0]); // an, ns, ar
    for (name, kind) in questions {
        encode_name(name, &mut out);
        out.extend_from_slice(&kind.to_be_bytes());
        out.extend_from_slice(&CLASS_IN.to_be_bytes());
    }
    out
}

#[derive(Debug, Clone, PartialEq)]
enum Rdata {
    Ptr(String),
    Srv { port: u16, target: String },
    Txt(Vec<(String, String)>),
    A(Ipv4Addr),
    Aaaa(Ipv6Addr),
}

#[derive(Debug, Clone, PartialEq)]
struct Record {
    name: String,
    data: Rdata,
}

/// Reads a possibly compressed name starting at `pos`; returns it and the position just past it
/// in the original stream.
fn read_name(buf: &[u8], mut pos: usize) -> Option<(String, usize)> {
    let mut name = String::new();
    let mut end = None;
    let mut jumps = 0;
    loop {
        let len = *buf.get(pos)? as usize;
        if len == 0 {
            pos += 1;
            break;
        }
        if len & 0xC0 == 0xC0 {
            let low = *buf.get(pos + 1)? as usize;
            end.get_or_insert(pos + 2);
            pos = ((len & 0x3F) << 8) | low;
            jumps += 1;
            if jumps > 16 {
                return None; // pointer loop
            }
            continue;
        }
        if len & 0xC0 != 0 {
            return None;
        }
        let label = buf.get(pos + 1..pos + 1 + len)?;
        name.push_str(&String::from_utf8_lossy(label));
        name.push('.');
        pos += 1 + len;
    }
    Some((name, end.unwrap_or(pos)))
}

fn read_u16(buf: &[u8], pos: usize) -> Option<u16> {
    Some(u16::from_be_bytes([*buf.get(pos)?, *buf.get(pos + 1)?]))
}

fn parse_txt(data: &[u8]) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < data.len() {
        let len = data[pos] as usize;
        let Some(entry) = data.get(pos + 1..pos + 1 + len) else {
            break;
        };
        pos += 1 + len;
        let text = String::from_utf8_lossy(entry);
        match text.split_once('=') {
            Some((key, value)) => out.push((key.to_string(), value.to_string())),
            None if !text.is_empty() => out.push((text.to_string(), String::new())),
            None => {}
        }
    }
    out
}

/// The records of a response (answers, authority and additional sections); `None` when the
/// packet is not a well-formed response.
fn parse_response(buf: &[u8]) -> Option<(u16, Vec<Record>)> {
    let id = read_u16(buf, 0)?;
    let flags = read_u16(buf, 2)?;
    if flags & 0x8000 == 0 {
        return None; // a query, not a response
    }
    let questions = read_u16(buf, 4)? as usize;
    let records = read_u16(buf, 6)? as usize
        + read_u16(buf, 8)? as usize
        + read_u16(buf, 10)? as usize;

    let mut pos = 12;
    for _ in 0..questions {
        let (_, next) = read_name(buf, pos)?;
        pos = next + 4;
    }

    let mut out = Vec::new();
    for _ in 0..records {
        let (name, next) = read_name(buf, pos)?;
        let kind = read_u16(buf, next)?;
        let rdlen = read_u16(buf, next + 8)? as usize;
        let start = next + 10;
        let data = buf.get(start..start + rdlen)?;
        pos = start + rdlen;

        let parsed = match kind {
            TYPE_PTR => read_name(buf, start).map(|(target, _)| Rdata::Ptr(target)),
            TYPE_SRV if rdlen >= 6 => read_name(buf, start + 6).map(|(target, _)| Rdata::Srv {
                port: u16::from_be_bytes([data[4], data[5]]),
                target,
            }),
            TYPE_TXT => Some(Rdata::Txt(parse_txt(data))),
            TYPE_A if rdlen == 4 => Some(Rdata::A(Ipv4Addr::new(data[0], data[1], data[2], data[3]))),
            TYPE_AAAA if rdlen == 16 => {
                let mut octets = [0u8; 16];
                octets.copy_from_slice(data);
                Some(Rdata::Aaaa(Ipv6Addr::from(octets)))
            }
            _ => None,
        };
        if let Some(data) = parsed {
            out.push(Record { name, data });
        }
    }
    Some((id, out))
}

// ---------- assembling a device from records ----------

fn same_name(a: &str, b: &str) -> bool {
    a.trim_end_matches('.').eq_ignore_ascii_case(b.trim_end_matches('.'))
}

/// Service instances named by a PTR record for `service`, whose SRV and TXT are not all known yet.
struct Assembly {
    devices: Vec<DiscoveredDevice>,
    /// Instances announced but still missing their SRV or TXT record.
    incomplete: Vec<String>,
}

fn assemble(records: &[Record], kind: ServiceKind, service: &str, ip: IpAddr) -> Assembly {
    let mut devices = Vec::new();
    let mut incomplete = Vec::new();
    let mut seen: Vec<String> = Vec::new();

    for instance in records.iter().filter_map(|r| match &r.data {
        Rdata::Ptr(target) if same_name(&r.name, service) => Some(target.clone()),
        _ => None,
    }) {
        // The same record can arrive twice (repeated or split answers).
        if seen.iter().any(|known| same_name(known, &instance)) {
            continue;
        }
        seen.push(instance.clone());
        let srv = records.iter().find_map(|r| match &r.data {
            Rdata::Srv { port, target } if same_name(&r.name, &instance) => Some((*port, target.clone())),
            _ => None,
        });
        let txt = records.iter().find_map(|r| match &r.data {
            Rdata::Txt(entries) if same_name(&r.name, &instance) => Some(entries),
            _ => None,
        });
        let (Some((port, hostname)), Some(txt)) = (srv, txt) else {
            incomplete.push(instance);
            continue;
        };
        let get = |key: &str| {
            txt.iter()
                .find(|(k, _)| k.eq_ignore_ascii_case(key))
                .map(|(_, v)| v.clone())
                .filter(|v| !v.is_empty())
        };
        devices.push(DiscoveredDevice {
            kind,
            service_name: instance.clone(),
            hostname: trim_local(&hostname),
            name: get("name"),
            identifier: get("identifier"),
            auth_tag: get("authTag"),
            // The address that just answered is, by construction, one that reaches the device.
            address: SocketAddr::new(ip, port),
        });
    }
    Assembly { devices, incomplete }
}

// ---------- the exchange ----------

/// Asks `hint` for the `kind` service and returns what it announces (usually one device).
///
/// Sends a PTR query, then, if the answer lacks the SRV or TXT record of an instance, a second
/// query for them. Nothing but packets from the queried address is looked at.
pub async fn query(hint: Hint, kind: ServiceKind, timeout: Duration) -> Result<Vec<DiscoveredDevice>, Error> {
    let bind: SocketAddr = if hint.addr.is_ipv4() {
        (Ipv4Addr::UNSPECIFIED, 0).into()
    } else {
        (Ipv6Addr::UNSPECIFIED, 0).into()
    };
    let socket = UdpSocket::bind(bind).await?;
    socket.connect(hint.addr).await?; // also filters incoming packets to the queried address

    let service = kind.service_type();
    let deadline = tokio::time::Instant::now() + timeout;
    let mut records: Vec<Record> = Vec::new();
    let mut asked_details = false;
    let mut buf = vec![0u8; 9000];

    socket.send(&encode_query(1, &[(service, TYPE_PTR)])).await?;

    loop {
        let received = tokio::select! {
            _ = tokio::time::sleep_until(deadline) => break,
            received = socket.recv(&mut buf) => received,
        };
        let len = match received {
            Ok(len) => len,
            // An ICMP "port unreachable" from a host that does not listen surfaces here.
            Err(e) => {
                log::debug!("direct mDNS query to {}: {e}", hint.addr);
                break;
            }
        };
        let Some((_, mut found)) = parse_response(&buf[..len]) else {
            continue;
        };
        records.append(&mut found);

        let assembly = assemble(&records, kind, service, hint.addr.ip());
        if assembly.incomplete.is_empty() && !assembly.devices.is_empty() {
            return Ok(assembly.devices);
        }
        if !asked_details && !assembly.incomplete.is_empty() {
            asked_details = true;
            let instance = assembly.incomplete[0].clone();
            let query = encode_query(2, &[(&instance, TYPE_SRV), (&instance, TYPE_TXT)]);
            socket.send(&query).await?;
        }
    }

    Ok(assemble(&records, kind, service, hint.addr.ip()).devices)
}

/// Direct counterpart of `discover`: both pairing services of the device at `hint`.
pub async fn discover_at(hint: Hint, timeout: Duration) -> Result<Vec<DiscoveredDevice>, Error> {
    let (manual, paired) = tokio::join!(
        query(hint, ServiceKind::ManualPairing, timeout),
        query(hint, ServiceKind::RemotePairing, timeout),
    );
    let mut devices = Vec::new();
    let mut first_error = None;
    for result in [manual, paired] {
        match result {
            Ok(mut found) => devices.append(&mut found),
            Err(e) => first_error = first_error.or(Some(e)),
        }
    }
    match (devices.is_empty(), first_error) {
        (true, Some(e)) => Err(e),
        _ => Ok(devices),
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    fn name(out: &mut Vec<u8>, n: &str) {
        encode_name(n, out);
    }

    /// A response like the one an Apple TV sends: PTR in the answers, SRV/TXT/A as additionals.
    fn response(with_details: bool) -> Vec<u8> {
        response_with(with_details, "abc", "tag")
    }

    pub(crate) fn response_with(with_details: bool, identifier: &str, auth_tag: &str) -> Vec<u8> {
        let service = "_remotepairing._tcp.local.";
        let instance = "AA-BB._remotepairing._tcp.local.";
        let mut out = Vec::new();
        out.extend_from_slice(&1u16.to_be_bytes());
        out.extend_from_slice(&0x8400u16.to_be_bytes());
        out.extend_from_slice(&0u16.to_be_bytes()); // questions
        out.extend_from_slice(&1u16.to_be_bytes()); // answers
        out.extend_from_slice(&0u16.to_be_bytes());
        out.extend_from_slice(&(if with_details { 3u16 } else { 0 }).to_be_bytes());

        // PTR _remotepairing._tcp.local. -> instance
        name(&mut out, service);
        out.extend_from_slice(&[0, 12, 0, 1, 0, 0, 0, 10]);
        let mut rd = Vec::new();
        name(&mut rd, instance);
        out.extend_from_slice(&(rd.len() as u16).to_be_bytes());
        out.extend_from_slice(&rd);

        if with_details {
            // SRV instance -> port 49152 on living-room.local.
            name(&mut out, instance);
            out.extend_from_slice(&[0x00, 33, 0x80, 1, 0, 0, 0, 10]);
            let mut rd = vec![0, 0, 0, 0, 0xC0, 0x00];
            name(&mut rd, "living-room.local.");
            out.extend_from_slice(&(rd.len() as u16).to_be_bytes());
            out.extend_from_slice(&rd);

            // TXT name=Salon, identifier=abc, authTag=tag
            name(&mut out, instance);
            out.extend_from_slice(&[0x00, 16, 0x80, 1, 0, 0, 0, 10]);
            let mut rd = Vec::new();
            let entries = [
                "name=Salon".to_string(),
                format!("identifier={identifier}"),
                format!("authTag={auth_tag}"),
            ];
            for entry in entries {
                rd.push(entry.len() as u8);
                rd.extend_from_slice(entry.as_bytes());
            }
            out.extend_from_slice(&(rd.len() as u16).to_be_bytes());
            out.extend_from_slice(&rd);

            // A living-room.local. -> 192.168.1.20
            name(&mut out, "living-room.local.");
            out.extend_from_slice(&[0x00, 1, 0x80, 1, 0, 0, 0, 10, 0, 4, 192, 168, 1, 20]);
        }
        out
    }

    #[test]
    fn query_has_one_question() {
        let q = encode_query(7, &[("_remotepairing._tcp.local.", TYPE_PTR)]);
        assert_eq!(&q[..2], &[0, 7]);
        assert_eq!(read_u16(&q, 4), Some(1));
        let (qname, next) = read_name(&q, 12).unwrap();
        assert_eq!(qname, "_remotepairing._tcp.local.");
        assert_eq!(read_u16(&q, next), Some(TYPE_PTR));
    }

    #[test]
    fn a_query_is_not_taken_for_a_response() {
        assert!(parse_response(&encode_query(1, &[("x.local.", TYPE_PTR)])).is_none());
    }

    #[test]
    fn garbage_is_ignored() {
        assert!(parse_response(&[0x80]).is_none());
        let mut truncated = response(true);
        truncated.truncate(40);
        assert!(parse_response(&truncated).is_none());
    }

    #[test]
    fn name_pointer_loops_are_rejected() {
        let mut buf = vec![0u8; 12];
        buf.extend_from_slice(&[0xC0, 12]); // points at itself
        assert!(read_name(&buf, 12).is_none());
    }

    #[test]
    fn full_response_builds_a_device() {
        let (_, records) = parse_response(&response(true)).unwrap();
        let ip: IpAddr = "192.168.1.20".parse().unwrap();
        let assembly = assemble(&records, ServiceKind::RemotePairing, "_remotepairing._tcp.local.", ip);
        assert!(assembly.incomplete.is_empty());
        let device = &assembly.devices[0];
        assert_eq!(device.name.as_deref(), Some("Salon"));
        assert_eq!(device.identifier.as_deref(), Some("abc"));
        assert_eq!(device.auth_tag.as_deref(), Some("tag"));
        assert_eq!(device.hostname, "living-room");
        assert_eq!(device.address, SocketAddr::new(ip, 49152));
    }

    #[test]
    fn ptr_alone_is_incomplete() {
        let (_, records) = parse_response(&response(false)).unwrap();
        let ip: IpAddr = "192.168.1.20".parse().unwrap();
        let assembly = assemble(&records, ServiceKind::RemotePairing, "_remotepairing._tcp.local.", ip);
        assert!(assembly.devices.is_empty());
        assert_eq!(assembly.incomplete.len(), 1);
    }

    #[test]
    fn other_services_are_not_matched() {
        let (_, records) = parse_response(&response(true)).unwrap();
        let ip: IpAddr = "192.168.1.20".parse().unwrap();
        let assembly = assemble(
            &records,
            ServiceKind::ManualPairing,
            "_remotepairing-manual-pairing._tcp.local.",
            ip,
        );
        assert!(assembly.devices.is_empty() && assembly.incomplete.is_empty());
    }

    /// A fake responder on loopback: answers the PTR query with the full response.
    #[tokio::test]
    async fn query_talks_to_a_responder() {
        let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        tokio::spawn(async move {
            let mut buf = [0u8; 512];
            let (_, peer) = server.recv_from(&mut buf).await.unwrap();
            server.send_to(&response(true), peer).await.unwrap();
        });
        let found = query(
            Hint::with_port(addr.ip(), addr.port()),
            ServiceKind::RemotePairing,
            Duration::from_secs(2),
        )
        .await
        .unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].identifier.as_deref(), Some("abc"));
        assert_eq!(found[0].address.port(), 49152);
    }

    /// Details missing from the first answer are asked for with a second query.
    #[tokio::test]
    async fn query_asks_for_missing_details() {
        let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        tokio::spawn(async move {
            let mut buf = [0u8; 512];
            let (_, peer) = server.recv_from(&mut buf).await.unwrap();
            server.send_to(&response(false), peer).await.unwrap();
            let (_, peer) = server.recv_from(&mut buf).await.unwrap();
            server.send_to(&response(true), peer).await.unwrap();
        });
        let found = query(
            Hint::with_port(addr.ip(), addr.port()),
            ServiceKind::RemotePairing,
            Duration::from_secs(2),
        )
        .await
        .unwrap();
        assert_eq!(found.len(), 1);
    }

    #[tokio::test]
    async fn silence_times_out_empty() {
        let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        let found = query(
            Hint::with_port(addr.ip(), addr.port()),
            ServiceKind::RemotePairing,
            Duration::from_millis(300),
        )
        .await
        .unwrap();
        assert!(found.is_empty());
        drop(server);
    }
}
