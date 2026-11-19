import json
from pathlib import Path
from subduck.models import CensorConfig

CONFIG_DIR = Path.home() / ".config" / "subduck"
PROFILES_FILE = CONFIG_DIR / "profiles.json"

DEFAULT_PROFILES = {
    "Medical & Illness Triggers": {
        "trigger_words": [
            "cancer", "tumor", "chemo", "chemotherapy", "stroke",
            "seizure", "hemorrhage", "overdose", "terminal"
        ],
        "safety_before_ms": 200,
        "safety_after_ms": 200,
        "ducking_mode": "mute",
        "ducking_level": 0.0,
        "censor_style": "asterisks",
        "strict_matching": True,
        "output_suffix": " trigger"
    },
    "Profanity & Swearing": {
        "trigger_words": [
            "ass", "asshole", "bastard", "bitch", "crap", "cunt",
            "damn", "dick", "fuck", "fucking", "motherfucker", "shit"
        ],
        "safety_before_ms": 150,
        "safety_after_ms": 150,
        "ducking_mode": "mute",
        "ducking_level": 0.0,
        "censor_style": "asterisks",
        "strict_matching": True,
        "output_suffix": " clean"
    },
    "Violence & Graphic Harm": {
        "trigger_words": [
            "blood", "bloody", "gore", "decapitate", "stab",
            "stabbing", "mutilate", "torture", "murder", "corpse"
        ],
        "safety_before_ms": 250,
        "safety_after_ms": 250,
        "ducking_mode": "duck_20db",
        "ducking_level": 0.1,
        "censor_style": "tag",
        "strict_matching": True,
        "output_suffix": " trigger"
    },
    "Phobias (Spiders & Snakes)": {
        "trigger_words": [
            "spider", "spiders", "tarantula", "arachnid", "snake",
            "snakes", "viper", "cobra", "rattlesnake"
        ],
        "safety_before_ms": 200,
        "safety_after_ms": 200,
        "ducking_mode": "mute",
        "ducking_level": 0.0,
        "censor_style": "block",
        "strict_matching": True,
        "output_suffix": " trigger"
    }
}


def load_all_profiles() -> dict:
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    profiles = dict(DEFAULT_PROFILES)
    if PROFILES_FILE.is_file():
        raw = PROFILES_FILE.read_text(encoding="utf-8")
        if raw.strip():
            user_data = json.loads(raw)
            if isinstance(user_data, dict):
                profiles.update(user_data)
    return profiles


def save_user_profile(name: str, config: CensorConfig) -> bool:
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
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def delete_user_profile(name: str) -> bool:
    profiles = load_all_profiles()
    if name not in profiles:
        return False
    del profiles[name]
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")
    return True
