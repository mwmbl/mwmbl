use mwmbl_core::hn_top_domains_filtered::DOMAINS;

#[test]
fn domains_parse() {
    assert!(DOMAINS.len() > 7000);
    assert!(DOMAINS.contains_key("paulgraham.com"));
}
