"""Skip identified decorative assets without affecting security or payment traffic."""

# Observed on the public portal. Do not broaden this to the CDN, all images,
# arbitrary scripts, fonts, CSS, or security-image/ resources.
DECORATIVE_ASSETS = [
    'https://cdn.myasnb.com.my/banner/hero-banner*.png*',
    'https://myasnb.oss-ap-southeast-3.aliyuncs.com/portal-rebrand/image/portal-pandubanner02_*.png*',
]


def configure_assets(driver, lean_assets=True):
    """Apply before first navigation; preserve the browser's normal cache."""
    driver.execute_cdp_cmd('Network.enable', {})
    driver.execute_cdp_cmd('Network.setCacheDisabled', {'cacheDisabled': False})
    driver.execute_cdp_cmd('Network.setBlockedURLs', {
        'urls': DECORATIVE_ASSETS if lean_assets else [],
    })
