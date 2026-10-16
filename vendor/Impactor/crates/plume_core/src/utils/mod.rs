use plist::Value;

mod certificate;
#[cfg(feature = "tweaks")]
mod macho;
mod provision;

pub use certificate::{CertificateIdentity, CertificatePolicy, RevokeMode};
#[cfg(feature = "tweaks")]
pub use macho::{MachO, MachOExt};
pub use provision::MobileProvision;

pub const TEAM_ID_REGEX: &str = r"^[A-Z0-9]{10}\.";

/// Concrete value of an entitlement string that holds a profile wildcard. `TEAMID.*` and the prefix
/// form `TEAMID.com.example.*` (a profile made for every app id under a prefix) both become
/// `TEAMID.<application id>`: the star stands for the whole identifier of the app, so a prefix must
/// not be repeated (`TEAMID.com.example.com.example.app` is refused by installd). Any other string
/// keeps the old rule: the star is replaced by the application id.
pub fn resolve_wildcard(value: &str, application_id: &str) -> String {
    if let Some(stem) = value.strip_suffix('*') {
        let team = stem.split('.').next().unwrap_or("");
        if stem.len() > team.len()
            && team.len() == 10
            && team.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
            && !stem.contains('*')
        {
            return format!("{team}.{application_id}");
        }
    }
    value.replace('*', application_id)
}

pub fn merge_entitlements(
    base: &mut plist::Dictionary,
    additions: &plist::Dictionary,
    new_team_id: &Option<String>,
    new_application_id: &Option<String>,
) {
    // replaces wildcards in base entitlements with new application id
    // aggressive approach though, lets just hope this works :)
    if let Some(new_app_id) = new_application_id {
        fn replace_wildcard(value: &mut Value, new_app_id: &str) {
            match value {
                Value::String(s) => {
                    if s.contains('*') {
                        *s = resolve_wildcard(s, new_app_id);
                    }
                }
                Value::Array(arr) => {
                    for item in arr.iter_mut() {
                        replace_wildcard(item, new_app_id);
                    }
                }
                Value::Dictionary(dict) => {
                    for v in dict.values_mut() {
                        replace_wildcard(v, new_app_id);
                    }
                }
                _ => {}
            }
        }
        for value in base.values_mut() {
            replace_wildcard(value, new_app_id);
        }
    }

    if let Some(Value::Array(groups)) = additions.get("keychain-access-groups") {
        base.insert(
            "keychain-access-groups".to_string(),
            Value::Array(groups.clone()),
        );
    }

    // remove anything that does not match XXXXXXXXXX. (for example, com.apple.token)
    // only XXXXXXXXXX.* is allowed on keychain-access-groups
    if let Some(Value::Array(groups)) = base.get_mut("keychain-access-groups") {
        let re = regex::Regex::new(TEAM_ID_REGEX).unwrap();
        groups.retain(|g| matches!(g, Value::String(s) if re.is_match(s)));
    }

    if let Some(new_id) = new_team_id {
        if let Some(Value::Array(groups)) = base.get_mut("keychain-access-groups") {
            for group in groups.iter_mut() {
                if let Value::String(s) = group {
                    let re = regex::Regex::new(TEAM_ID_REGEX).unwrap();
                    if re.is_match(s) {
                        *s = format!("{}.{}", new_id, &s[11..]);
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use plist::Dictionary;

    #[test]
    fn full_wildcard_becomes_the_application_id() {
        assert_eq!(
            resolve_wildcard("ABCDE12345.*", "com.example.app"),
            "ABCDE12345.com.example.app"
        );
    }

    #[test]
    fn prefix_wildcard_is_not_repeated() {
        // a profile for TEAMID.com.example.* must give TEAMID.com.example.app, not a doubled prefix
        assert_eq!(
            resolve_wildcard("ABCDE12345.com.example.*", "com.example.app"),
            "ABCDE12345.com.example.app"
        );
    }

    #[test]
    fn other_strings_keep_the_old_rule() {
        assert_eq!(resolve_wildcard("*", "com.example.app"), "com.example.app");
        assert_eq!(resolve_wildcard("group.*", "com.example.app"), "group.com.example.app");
        assert_eq!(resolve_wildcard("com.example.app", "x"), "com.example.app");
        // a star that is not the last character is not a profile wildcard
        assert_eq!(
            resolve_wildcard("ABCDE12345.*.shared", "com.example.app"),
            "ABCDE12345.com.example.app.shared"
        );
    }

    #[test]
    fn merge_resolves_application_identifier_and_keychain_groups() {
        let mut base = Dictionary::new();
        base.insert(
            "application-identifier".into(),
            Value::String("ABCDE12345.com.example.*".into()),
        );
        base.insert(
            "keychain-access-groups".into(),
            Value::Array(vec![Value::String("ABCDE12345.*".into())]),
        );
        merge_entitlements(
            &mut base,
            &Dictionary::new(),
            &Some("ABCDE12345".into()),
            &Some("com.example.app".into()),
        );
        assert_eq!(
            base.get("application-identifier").and_then(Value::as_string),
            Some("ABCDE12345.com.example.app")
        );
        let groups = base.get("keychain-access-groups").and_then(Value::as_array).unwrap();
        assert_eq!(
            groups[0].as_string(),
            Some("ABCDE12345.com.example.app")
        );
    }
}
