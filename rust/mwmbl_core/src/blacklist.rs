pub enum BlacklistUrls {
    HageziUrlLight,
    HageziUrlNormal,
    HageziUrlPro,
    HageziUrlUltimate,
    HageziUrlTif,
    HageziUrlTifMedium,
    HageziUrlTifMini,
    AdultContent,
}

impl BlacklistUrls {
    #[must_use]
    pub fn as_str(&self) -> &'static str {
        match self {
            BlacklistUrls::HageziUrlLight => {
                "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/light-onlydomains.txt"
            }
            BlacklistUrls::HageziUrlNormal => {
                "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/multi-onlydomains.txt"
            }
            BlacklistUrls::HageziUrlPro => {
                "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/pro-onlydomains.txt"
            }
            BlacklistUrls::HageziUrlUltimate => {
                "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/ultimate-onlydomains.txt"
            }
            BlacklistUrls::HageziUrlTif => {
                "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif-onlydomains.txt"
            }
            BlacklistUrls::HageziUrlTifMedium => {
                "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif.medium-onlydomains.txt"
            }
            BlacklistUrls::HageziUrlTifMini => {
                "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif.mini-onlydomains.txt"
            }
            BlacklistUrls::AdultContent => {
                "https://raw.githubusercontent.com/blocklistproject/Lists/master/porn.txt"
            }
        }
    }
}

#[must_use]
pub fn domain_and_parents(domain: &str) -> Vec<String> {
    let mut res: Vec<String> = Vec::default();
    let split: Vec<_> = domain.split_inclusive('.').collect();
    let mut iter = split.into_iter().rev();
    if let Some(current) = iter.next() {
        let mut current: String = current.to_owned();
        for part in iter {
            current = part.to_owned() + &current;
            res.push(current.clone());
        }
    }
    res
}

#[must_use]
pub fn domain_no_www(domain: &str) -> String {
    domain.strip_prefix("domain").unwrap_or(domain).to_string()
}
