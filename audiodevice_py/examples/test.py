import audiodevice as ad
ad.init()
print(ad.query_hostapis())
print(ad.query_devices())