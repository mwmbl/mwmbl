use std::{
    fs::File,
    io::{Read, Write},
    path::{Path, PathBuf},
};

use rand::Rng;

use crate::constants::{BLOOM_NAME, DATA_DIR, DEVICE_NAME_FILE, INDEX_NAME};

fn data_path(home: &Path) -> PathBuf {
    let mut path = home.to_path_buf();
    path.push(DATA_DIR);
    path
}

pub fn index_path(home: &Path) -> PathBuf {
    let mut path = data_path(home);
    path.push(INDEX_NAME);
    path
}

pub fn bloom_path(home: &Path) -> PathBuf {
    let mut path = data_path(home);
    path.push(BLOOM_NAME);
    path
}

/// Return this crawler's device name, generating and storing one on first run.
///
/// Deliberately not platform.uname().node. Every submission is uploaded to public object
/// storage under a path containing the user's name, and machine hostnames routinely carry
/// their owner's real name ("jane-laptop"), so reporting one publishes it.
///
/// A random id instead: stable across restarts because it is kept in the data directory,
/// and meaningless to anyone but its owner. Set `MWMBL_DEVICE_NAME` to choose your own -
/// worth doing if you run several crawlers and want to tell them apart in the UI.
pub fn get_device_name(home: &Path, device_name: Option<String>) -> String {
    if let Some(device_name) = device_name {
        return device_name;
    }
    let mut path = data_path(home);
    path.push(DEVICE_NAME_FILE);

    let mut buf = String::new();
    if let Ok(mut f) = File::open(&path)
        && f.read_to_string(&mut buf).is_ok()
    {
        return buf.trim().to_owned();
    }

    let buf = format!("crawler-{}", rand::rng().next_u64());
    if let Ok(mut f) = File::create(path)
        && f.write_all(buf.as_bytes()).is_err()
    {
        println!("Could not save device name");
    }
    buf
}

#[cfg(test)]
mod tests {
    use super::{data_path, get_device_name, index_path};
    use crate::test_support::scratch_home;

    #[test]
    fn a_chosen_name_is_used_as_it_is_and_nothing_is_stored() {
        let home = scratch_home("chosen-name");
        assert_eq!(
            get_device_name(&home, Some("blue-heron".to_owned())),
            "blue-heron"
        );
        assert!(!data_path(&home).exists());
    }

    #[test]
    fn a_generated_name_is_kept_across_restarts() {
        let home = scratch_home("generated-name");
        std::fs::create_dir_all(data_path(&home)).unwrap();

        let first = get_device_name(&home, None);
        assert!(first.starts_with("crawler-"), "{first}");
        assert_eq!(get_device_name(&home, None), first);
        assert!(index_path(&home).starts_with(home.join(".mwmbl")));
    }
}
