import configparser
from pathlib import Path
from typing import Optional, List

CONFIG_FILE_NAME = "config.ini"

class ConfigError(Exception):
    """Custom exception for configuration errors."""
    pass

def load_config(config_dir: Path = Path('.'), profile: Optional[str] = None) -> configparser.ConfigParser:
    """
    Loads the configuration from the config.ini file.

    Args:
        config_dir: The directory containing the config.ini file.
                    Defaults to the current directory.
        profile: If provided, validate that the named profile section exists
                 with required fields. If not provided, just load and return
                 the config (useful for GUI discovery of available profiles).

    Returns:
        A ConfigParser object with the loaded configuration.

    Raises:
        ConfigError: If the config.ini file is not found or is invalid.
    """
    config_path = config_dir / CONFIG_FILE_NAME
    if not config_path.is_file():
        raise ConfigError(
            f"Configuration file '{CONFIG_FILE_NAME}' not found in '{config_dir}'. "
            f"Please copy '{CONFIG_FILE_NAME}.template' to '{CONFIG_FILE_NAME}' "
            "and fill in your details."
        )

    config = configparser.ConfigParser()
    try:
        config.read(config_path)
    except configparser.Error as e:
        raise ConfigError(f"Error reading configuration file: {e}") from e

    if profile:
        profile_section = f'Profile.{profile}'
        if profile_section not in config:
            raise ConfigError(f"Profile '{profile}' not found. Expected [{profile_section}] section in config file.")
        prof = config[profile_section]
        if not prof.get('username') or not prof.get('password') or not prof.get('security_phrase'):
            raise ConfigError(f"Missing username, password, or security_phrase under [{profile_section}].")

    return config

def get_profiles(config: configparser.ConfigParser) -> dict:
    """Discover all [Profile.*] sections and return as dict of dicts."""
    profiles = {}
    for section in config.sections():
        if section.startswith('Profile.'):
            name = section[len('Profile.'):]
            profiles[name] = dict(config[section])
    return profiles

def get_profile_names(config: configparser.ConfigParser) -> List[str]:
    """Return list of available profile names from config."""
    return [s[len('Profile.'):] for s in config.sections() if s.startswith('Profile.')]

def get_funds_list(config: configparser.ConfigParser) -> List[str]:
    """
    Retrieves and parses the comma-separated list of funds from the config.

    Args:
        config: The loaded ConfigParser object.

    Returns:
        A list of fund names. Returns an empty list if the setting is not found or empty.
    """
    funds_string = config.get('Settings', 'funds_to_try', fallback='').strip()
    if not funds_string:
        return []
    # Split by comma and strip whitespace from each fund name
    funds = [fund.strip() for fund in funds_string.split(',') if fund.strip()]
    return funds

def get_email_config(config: configparser.ConfigParser) -> dict:
    """
    Retrieves email configuration settings from the config.

    Args:
        config: The loaded ConfigParser object.

    Returns:
        A dictionary containing email settings, or empty dict if Email section doesn't exist.
    """
    if 'Email' not in config:
        return {}
    
    email_section = config['Email']
    
    # Check if basic email settings are provided
    smtp_server = email_section.get('smtp_server', '').strip()
    sender_email = email_section.get('sender_email', '').strip()
    sender_password = email_section.get('sender_password', '').strip()
    recipient_email = email_section.get('recipient_email', '').strip()
    
    # If any required field is missing, return empty config (disables email)
    if not all([smtp_server, sender_email, sender_password, recipient_email]):
        return {}
    
    # Parse recipient emails (support comma-separated list)
    recipient_emails = [email.strip() for email in recipient_email.split(',') if email.strip()]
    
    return {
        'smtp_server': smtp_server,
        'smtp_port': int(email_section.get('smtp_port', '587')),
        'sender_email': sender_email,
        'sender_password': sender_password,
        'recipient_emails': recipient_emails,  # Changed to plural and list
        'send_on_success': email_section.getboolean('send_on_success', fallback=True),
        'send_on_failure': email_section.getboolean('send_on_failure', fallback=False),
        'email_subject': email_section.get('email_subject', 'ASNB Purchase Notification').strip()
    }

def get_session_refresh_interval(config: configparser.ConfigParser) -> int:
    """
    Retrieves the session refresh interval from the config.
    
    Args:
        config: The loaded ConfigParser object.
        
    Returns:
        The number of fund attempts before refreshing session (default: 6)
    """
    return config.getint('Settings', 'session_refresh_interval', fallback=6)

if __name__ == "__main__":
    try:
        cfg = load_config()
        print("Config loaded successfully.")
        profiles = get_profiles(cfg)
        print(f"Available profiles: {list(profiles.keys())}")
        for name, prof in profiles.items():
            print(f"  {name}: username={prof.get('username', '?')}")
        funds = get_funds_list(cfg)
        print(f"Funds to try: {funds}")
    except ConfigError as e:
        print(f"Configuration Error: {e}")
