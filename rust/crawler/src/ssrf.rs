//! Keeps the crawler off the volunteer's own network. Every fetch of a third-party site goes
//! through a client whose resolver refuses hostnames that resolve to private, loopback,
//! link-local or otherwise non-public addresses. Checking at resolution time means the address
//! checked is the address connected to, so redirects, robots.txt fetches and DNS rebinding are
//! all covered. reqwest never asks the resolver about IP literals, so the redirect policy
//! refuses those, and `crawl` refuses them as the first URL.

use std::{
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    sync::Arc,
};

use hickory_resolver::{
    TokioResolver,
    config::{GOOGLE, LookupIpStrategy, ResolverConfig},
    net::runtime::TokioRuntimeProvider,
};
use reqwest::{
    dns::{Addrs, Name, Resolve, Resolving},
    redirect::Policy,
};

use crate::constants::MAX_REDIRECTS;

#[must_use]
pub fn is_public_ipv4(ip: Ipv4Addr) -> bool {
    let [a, b, c, _] = ip.octets();
    let shared_address_space = a == 100 && (64..128).contains(&b);
    let ietf_protocol_assignments = a == 192 && b == 0 && c == 0;
    let benchmarking = a == 198 && (b == 18 || b == 19);
    let reserved = a >= 240;
    !(a == 0
        || ip.is_private()
        || ip.is_loopback()
        || ip.is_link_local()
        || ip.is_multicast()
        || ip.is_broadcast()
        || ip.is_documentation()
        || shared_address_space
        || ietf_protocol_assignments
        || benchmarking
        || reserved)
}

/// Public IPv6 is global unicast (`2000::/3`) outside the special-purpose blocks. Addresses that
/// embed an IPv4 address are judged by it, so `::ffff:127.0.0.1` counts as loopback.
#[must_use]
pub fn is_public_ipv6(ip: Ipv6Addr) -> bool {
    if let Some(v4) = ip.to_ipv4_mapped() {
        return is_public_ipv4(v4);
    }
    let segments = ip.segments();
    let embedded_v4 =
        |high: u16, low: u16| Ipv4Addr::from((u32::from(high) << 16) | u32::from(low));
    let nat64 = segments[..6] == [0x64, 0xff9b, 0, 0, 0, 0];
    if nat64 {
        return is_public_ipv4(embedded_v4(segments[6], segments[7]));
    }
    let six_to_four = segments[0] == 0x2002;
    if six_to_four {
        return is_public_ipv4(embedded_v4(segments[1], segments[2]));
    }
    let global_unicast = segments[0] & 0xe000 == 0x2000;
    let ietf_protocol_assignments = segments[0] == 0x2001 && segments[1] < 0x200;
    let documentation = segments[0] == 0x2001 && segments[1] == 0xdb8;
    global_unicast && !ietf_protocol_assignments && !documentation
}

#[must_use]
pub fn is_public(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => is_public_ipv4(v4),
        IpAddr::V6(v6) => is_public_ipv6(v6),
    }
}

/// Resolves with `inner`, and fails the lookup if any address it returns is not public.
pub struct PublicAddressResolver {
    inner: Arc<dyn Resolve>,
}

impl PublicAddressResolver {
    pub fn new(inner: Arc<dyn Resolve>) -> PublicAddressResolver {
        PublicAddressResolver { inner }
    }

    /// The system's resolver, as reqwest's own `hickory-dns` resolver configures it.
    pub fn system() -> anyhow::Result<PublicAddressResolver> {
        let mut builder = TokioResolver::builder_tokio().unwrap_or_else(|_| {
            TokioResolver::builder_with_config(
                ResolverConfig::udp_and_tcp(&GOOGLE),
                TokioRuntimeProvider::default(),
            )
        });
        builder.options_mut().ip_strategy = LookupIpStrategy::Ipv6AndIpv4;
        let resolver = builder.build()?;
        Ok(PublicAddressResolver::new(Arc::new(HickoryResolver(
            resolver,
        ))))
    }
}

impl Resolve for PublicAddressResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let host = name.as_str().to_owned();
        let lookup = self.inner.resolve(name);
        Box::pin(async move {
            let addrs: Vec<SocketAddr> = lookup.await?.collect();
            if let Some(blocked) = addrs.iter().find(|addr| !is_public(addr.ip())) {
                return Err(
                    format!("{host} resolves to non-public address {}", blocked.ip()).into(),
                );
            }
            let addrs: Addrs = Box::new(addrs.into_iter());
            Ok(addrs)
        })
    }
}

struct HickoryResolver(TokioResolver);

impl Resolve for HickoryResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let resolver = self.0.clone();
        Box::pin(async move {
            let lookup = resolver.lookup_ip(name.as_str()).await?;
            let addrs: Vec<SocketAddr> = lookup.iter().map(|ip| SocketAddr::new(ip, 0)).collect();
            let addrs: Addrs = Box::new(addrs.into_iter());
            Ok(addrs)
        })
    }
}

/// Follows at most `MAX_REDIRECTS + 1` redirects, and none to an IP literal, which would
/// skip the resolver.
pub fn redirect_policy() -> Policy {
    Policy::custom(|attempt| {
        if attempt.url().domain().is_none() {
            let error = format!(
                "refusing redirect to {}, which has no domain",
                attempt.url()
            );
            attempt.error(error)
        } else if attempt.previous().len() > MAX_REDIRECTS + 1 {
            attempt.error("too many redirects")
        } else {
            attempt.follow()
        }
    })
}

#[cfg(test)]
mod tests {
    use std::{net::IpAddr, str::FromStr, sync::Arc};

    use reqwest::{
        Client,
        dns::{Addrs, Name, Resolve, Resolving},
    };

    use super::{PublicAddressResolver, is_public, redirect_policy};
    use crate::{
        retrieve::fetch,
        test_support::{TestServer, html, redirect},
    };

    fn public(address: &str) -> bool {
        is_public(IpAddr::from_str(address).unwrap())
    }

    #[test]
    fn ordinary_internet_addresses_are_public() {
        for address in [
            "1.1.1.1",
            "8.8.8.8",
            "93.184.216.34",
            "100.63.255.255",
            "100.128.0.0",
            "172.15.255.255",
            "172.32.0.0",
            "2606:4700:4700::1111",
            "2a00:1450:4009:81f::200e",
            "::ffff:8.8.8.8",
            "64:ff9b::808:808",
            "2002:808:808::1",
        ] {
            assert!(public(address), "{address}");
        }
    }

    #[test]
    fn internal_and_special_addresses_are_not_public() {
        for address in [
            "0.0.0.0",
            "0.1.2.3",
            "10.0.0.1",
            "100.64.0.1",
            "100.127.255.255",
            "127.0.0.1",
            "127.255.255.254",
            "169.254.169.254",
            "172.16.0.1",
            "172.31.255.255",
            "192.0.0.8",
            "192.0.2.1",
            "192.168.1.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "239.255.255.250",
            "240.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:169.254.169.254",
            "64:ff9b::7f00:1",
            "2002:c0a8:101::1",
            "fc00::1",
            "fd12:3456:789a::1",
            "fe80::1",
            "fec0::1",
            "ff02::1",
            "100::1",
            "2001:db8::1",
            "2001::1",
        ] {
            assert!(!public(address), "{address}");
        }
    }

    /// Stands in for DNS: every name resolves to 127.0.0.1.
    struct Loopback;

    impl Resolve for Loopback {
        fn resolve(&self, _name: Name) -> Resolving {
            Box::pin(async {
                let addrs: Addrs = Box::new(std::iter::once(([127, 0, 0, 1], 0).into()));
                Ok(addrs)
            })
        }
    }

    #[tokio::test]
    async fn a_hostname_resolving_to_loopback_is_not_fetched() {
        let server = TestServer::start(vec![("/", html(200, "secret"))]).await;
        let client = Client::builder()
            .no_proxy()
            .dns_resolver(PublicAddressResolver::new(Arc::new(Loopback)))
            .build()
            .unwrap();

        assert!(fetch(&client, &server.url("/")).await.is_err());
        assert!(server.requests().is_empty());
    }

    #[tokio::test]
    async fn the_resolver_passes_through_public_addresses() {
        let resolver = PublicAddressResolver::new(Arc::new(Public));
        let addrs: Vec<_> = resolver
            .resolve(Name::from_str("example.com").unwrap())
            .await
            .unwrap()
            .collect();
        assert_eq!(addrs, vec![([93, 184, 216, 34], 0).into()]);
    }

    struct Public;

    impl Resolve for Public {
        fn resolve(&self, _name: Name) -> Resolving {
            Box::pin(async {
                let addrs: Addrs = Box::new(std::iter::once(([93, 184, 216, 34], 0).into()));
                Ok(addrs)
            })
        }
    }

    #[tokio::test]
    async fn redirects_to_ip_literals_are_not_followed() {
        let target = TestServer::start(vec![("/", html(200, "secret"))]).await;
        let location = format!("http://127.0.0.1:{}/", target.url("/").port().unwrap());
        let origin = TestServer::start(vec![("/", redirect(&location))]).await;
        let client = Client::builder()
            .no_proxy()
            .redirect(redirect_policy())
            .build()
            .unwrap();

        assert!(fetch(&client, &origin.url("/")).await.is_err());
        assert!(target.requests().is_empty());
    }
}
