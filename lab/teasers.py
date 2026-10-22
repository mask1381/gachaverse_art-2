from __future__ import annotations

"""Name-free teaser lines per cluster (SFW and NSFW share this file).

The title line already carries the character and the game, so a teaser never repeats them.
No cluster = no teaser. Pick history lives in runtime["recentCaptions"] under "teaser:<cluster>".
"""

import random

# NSFW cluster keys (caption_nsfw.CLUSTER_KEYWORDS) -> cluster names below. "waifu" (default) is absent on purpose: no teaser.
NSFW_TO_CLUSTER = {
    "maid": "Maid",
    "selfies": "Selfies",
    "swimsuit": "Swimsuit",
    "uniform": "Uniform",
    "milf": "Mature",
    "bathing": "Bathing",
    "ass": "Backview",
    "oppai": "Figure",
}

CLUSTER_TEASERS = {
    "Maid": [
        "Your maid is ready to serve 🎀",
        "Tea, snacks, and smiles ☕💕",
        "Welcome home ✨",
        "Cutest maid at the cafe 😍",
        "Frills, ribbons, and a warm welcome 🎀",
        "Service with a gentle smile 😊",
        "Dinner is served, master 🍰",
        "Spotless, sweet, and always on time 💕",
    ],
    "Uniform": [
        "The bell just rang 🔔",
        "Class is over, come play 🎒",
        "The prettiest student in school 😍",
        "Sit next to her, please 📚💕",
        "Last period never felt this short ✨",
        "Walking home together? 🌸",
        "Perfect attendance, perfect look 🏫",
        "Rooftop lunch, just us two 🍱",
    ],
    "Swimsuit": [
        "Summer is here ☀️",
        "Take her to the beach 🏖️",
        "Sun, sea, and smiles 😍",
        "Pool day with her 💦",
        "Sunscreen is on, the day is ours 🌴",
        "Waves, ice cream, and good vibes 🍦",
        "Last splash before sunset 🌅",
        "Beach day hits different with her 😎",
    ],
    "Selfies": [
        "She took a pic for you 📸",
        "Smile for the camera 😊",
        "Best angle found ✨",
        "New photo just dropped 🤳",
        "Caught mid-pose, still perfect 📸",
        "One more shot, then we go 💕",
        "No filter needed ✨",
        "Sent straight to your gallery 🤳",
    ],
    "Leotard": [
        "Hop into the night 🐰",
        "The cutest bunny in town 💕",
        "Long ears, big charm ✨",
        "Waiting for you 🌙",
        "Carrots are overrated, she's better 🥕",
        "Nothing but a playful wink 😉",
        "The stage lights found her 🎪",
        "Midnight shift, maximum charm 🌙",
    ],
    "Kemonomimi": [
        "She's part cat, all cute 🐾",
        "Following her tail today ✨",
        "A little wild, a little sweet 💕",
        "Someone left the door open 🐾😊",
        "Ears up, she heard you coming 👂",
        "Softest fur in the whole gallery 🦊",
        "Purr-fect timing 🐱",
        "Don't forget the headpats 🐾💕",
    ],
    "Fantasy": [
        "Something magical about her ✨",
        "Legends don't do her justice 🌙",
        "A being beyond this world 🍃",
        "Not from around here ✨",
        "Born under a different moon 🌙",
        "Spells, wings, and a quiet smile 🪽",
        "The old stories were right 📜",
        "Step into her realm 🔮",
    ],
    "Kimono": [
        "Timeless beauty, modern charm ✨",
        "Dressed for the festival 🏮",
        "Old world grace, new world charm 🎋",
        "Every fold tells a story 🎐",
        "Lanterns glow brighter near her 🏮",
        "Summer night, fireworks, and her 🎆",
        "Elegance in every detail 🌸",
        "Tradition never looked this good ✨",
    ],
    "Legwear": [
        "Details make the difference 🖤",
        "Elegant from head to toe ✨",
        "A little extra, a lot of charm 😳",
        "Simple but striking 🎀",
        "Style that goes the extra mile 👣",
        "Clean lines, confident walk ✨",
        "Small touches, big impression 🖤",
        "Dressed to impress, quietly 😌",
    ],
    "Bathing": [
        "Steam, warm water, and calm ♨️",
        "Time to unwind 🛁",
        "The perfect end to a long day ✨",
        "Hot spring escape ♨️💕",
        "Slow evening, no rush 🌙",
        "Warm towels and quiet moments 🛁",
        "Relax mode on 😌",
        "Onsen night, just breathe ♨️",
    ],
    "Mature": [
        "Grace that only comes with time 💐",
        "Warm smile, steady charm ✨",
        "Elegant and effortless 😊",
        "The calm one in the room 🍷",
        "Gentle, graceful, unforgettable 💕",
        "Class never goes out of style ✨",
        "Mature charm, perfectly poised 🌹",
        "Big sister energy, all heart 💖",
    ],
    "Backview": [
        "A look over the shoulder 👀",
        "Caught her turning around 😊",
        "The view from behind ✨",
        "Walking away never looked so good 💕",
        "Just a glance back 😏",
        "Pretty from every angle 😍",
        "She knows you're watching 👀✨",
        "Turn around, one more time 💖",
    ],
    "Figure": [
        "Soft light, striking figure ✨",
        "Curves in all the right places 😍",
        "Confidence looks good on her 💕",
        "A figure worth a second look 😳",
        "Golden hour suits her 🌇",
        "Posed like she owns the room ✨",
        "Beauty with a bold side 💖",
        "Stunning, and she knows it 😏",
    ],
}


def pick_teaser(cluster: str | None, memory: dict) -> str:
    """One teaser for the cluster, avoiding the last few used. '' when there is no cluster."""
    pool = CLUSTER_TEASERS.get(cluster or "")
    if not pool:
        return ""
    slot = f"teaser:{cluster}"
    used = list(memory.get(slot) or [])
    fresh = [x for x in pool if x not in used] or list(pool)
    chosen = random.choice(fresh)
    used.append(chosen)
    keep = min(6, len(pool) - 1)
    memory[slot] = used[-keep:] if keep else []
    return chosen
