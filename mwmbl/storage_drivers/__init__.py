from mwmbl.settings import STORAGE_DRIVER

if STORAGE_DRIVER=="s3":
    from mwmbl.storage_drivers.s3_driver import upload, get_batches_for_prefix, get_subfolders
elif STORAGE_DRIVER=="local":
    from mwmbl.storage_drivers.local_driver import upload, get_batches_for_prefix, get_subfolders
else:
    raise ImportError("MWMBL_STORAGE_DRIVER must be either 's3' or 'local")