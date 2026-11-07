import os
import json
from pathlib import Path
from typing import Dict, Any, List
from subduck.models import CensorConfig

CONFIG_DIR = Path.home() / ".config" / "subduck"
PROFILES_FILE = CONFIG_DIR / "profiles.json"

DEFAULT_PROFILES: Dict[str, Dict[str, Any]] = {
    "Medical & Illness Triggers": {
        "trigger_words": ["cancer", "tumor", "chemo", "chemotherapy", "stroke", "seizure", "hemorrhage", "overdose", "terminal"],
        "safety_before_ms": 200,
        "safety_after_ms": 200,
        "ducking_mode": "mute",
        "ducking_level": 0.0,
        "censor_style": "asterisks",
        "strict_matching": True,
        "output_suffix": " trigger"
    },
    "Profanity & Swearing": {
        "trigger_words": ["ass", "asshole", "bastard", "bitch", "crap", "cunt", "damn", "dick", "fuck", "fucking", "motherfucker", "shit"],
        "safety_before_ms": 150,
        "safety_after_ms": 150,
        "ducking_mode": "mute",
        "ducking_level": 0.0,
        "censor_style": "asterisks",
        "strict_matching": True,
        "output_suffix": " clean"
    },
    "Violence & Graphic Harm": {
        "trigger_words": ["blood", "bloody", "gore", "decapitate", "stab", "stabbing", "mutilate", "torture", "murder", "corpse"],
        "safety_before_ms": 250,
        "safety_after_ms": 250,
        "ducking_mode": "duck_20db",
        "ducking_level": 0.1,
        "censor_style": "tag",
        "strict_matching": True,
        "output_suffix": " trigger"
    },
    "Phobias (Spiders & Snakes)": {
        "trigger_words": ["spider", "spiders", "tarantula", "arachnid", "snake", "snakes", "viper", "cobra", "rattlesnake"],
        "safety_before_ms": 200,
        "safety_after_ms": 200,
        "ducking_mode": "mute",
        "ducking_level": 0.0,
        "censor_style": "block",
        "strict_matching": True,
        "output_suffix": " trigger"
    }
}

def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def load_all_profiles() -> Dict[str, Dict[str, Any]]:
    """Load user profiles merged with built-in presets."""
    ensure_config_dir()
    profiles = dict(DEFAULT_PROFILES)
    if PROFILES_FILE.is_file():
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                user_profiles = json.load(f)
                profiles.update(user_profiles)
        except Exception:
            pass
    return profiles

def save_user_profile(name: str, config: CensorConfig) -> bool:
    """Save or update a named profile."""
    ensure_config_dir()
    profiles = load_all_profiles()
    profiles[name] = {
        "trigger_words": config.trigger_words,
        "safety_before_ms": config.safety_before_ms,
        "safety_after_ms": config.safety_after_ms,
        "ducking_mode": config.ducking_mode,
        "ducking_level": config.ducking_level,
        "censor_style": config.censor_style,
        "strict_matching": config.strict_matching,
        "output_suffix": config.output_suffix
    }
    try:
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

def delete_user_profile(name: str) -> bool:
    """Delete a profile from storage."""
    ensure_config_dir()
    profiles = load_all_profiles()
    if name in profiles:
        del profiles[name]
        try:
            with open(PROFILES_FILE, "w", encoding="utf-8") as f:
                json.dump(profiles, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False
    return False
