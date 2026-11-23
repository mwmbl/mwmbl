use std::path::{Path, PathBuf};

use rand::Rng;
use tokio::{
    fs::File,
    io::{AsyncReadExt, AsyncWriteExt},
};

use crate::constants::{BLOOM_NAME, DATA_DIR, DEVICE_NAME_FILE, HEALTH_FILE_NAME, INDEX_NAME};

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
pub async fn get_device_name(home: &Path, device_name: Option<String>) -> String {
    // Compose passes MWMBL_DEVICE_NAME through empty when it is unset, and that is unset.
    if let Some(device_name) = device_name
        && !device_name.is_empty()
    {
        return device_name;
    }
    let mut path = data_path(home);
    path.push(DEVICE_NAME_FILE);

    let mut buf = String::new();
    if let Ok(mut f) = File::open(&path).await
        && f.read_to_string(&mut buf).await.is_ok()
    {
        return buf.trim().to_owned();
    }

    let buf = format!("crawler-{}", rand::rng().next_u64());
    if let Ok(mut f) = File::create(path).await
        && f.write_all(buf.as_bytes()).await.is_err()
    {
        println!("Could not save device name");
    }
    buf
}

pub async fn healthcheck(home: &Path) -> anyhow::Result<()> {
    let mut path = data_path(home);
    path.push(HEALTH_FILE_NAME);
    // Writing to the file is what updates its modification time.
    tokio::fs::write(&path, b"ok").await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime};

    use super::{data_path, get_device_name, healthcheck, index_path};
    use crate::{constants::HEALTH_FILE_NAME, test_support::scratch_home};

    #[tokio::test]
    async fn a_stale_health_file_is_brought_up_to_date() {
        let home = scratch_home("health");
        std::fs::create_dir_all(data_path(&home)).unwrap();
        let path = data_path(&home).join(HEALTH_FILE_NAME);
        let an_hour_ago = SystemTime::now() - Duration::from_hours(1);
        std::fs::File::create(&path)
            .unwrap()
            .set_modified(an_hour_ago)
            .unwrap();

        healthcheck(&home).await.unwrap();

        let modified = std::fs::metadata(&path).unwrap().modified().unwrap();
        assert!(modified.elapsed().unwrap() < Duration::from_mins(1));
    }

    #[tokio::test]
    async fn a_chosen_name_is_used_as_it_is_and_nothing_is_stored() {
        let home = scratch_home("chosen-name");
        assert_eq!(
            get_device_name(&home, Some("blue-heron".to_owned())).await,
            "blue-heron"
        );
        assert!(!data_path(&home).exists());
    }

    #[tokio::test]
    async fn a_generated_name_is_kept_across_restarts() {
        let home = scratch_home("generated-name");
        std::fs::create_dir_all(data_path(&home)).unwrap();

        let first = get_device_name(&home, None).await;
        assert!(first.starts_with("crawler-"), "{first}");
        assert_eq!(get_device_name(&home, None).await, first);
        assert_eq!(get_device_name(&home, Some(String::new())).await, first);
        assert!(index_path(&home).starts_with(home.join(".mwmbl")));
    }
}
