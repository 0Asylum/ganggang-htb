import io
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).parent / "assets"

BG_COLOR    = (20, 29, 44)
WHITE       = (255, 255, 255)
DIM_WHITE   = (170, 180, 195)
HTB_GREEN   = (159, 239, 0)
GLOBAL_RED  = (237, 28,  36)
TEAM_PURPLE = (148, 0,  148)
SEPARATOR   = (50,  65,  90)
BAR_BG      = (40,  55,  80)
ROW_ALT     = (26,  37,  56)
GOLD        = (255, 200, 60)
SILVER      = (200, 210, 220)
BRONZE      = (205, 140, 80)

LB_PURPLE   = (168, 60, 210)  # brighter than TEAM_PURPLE for legibility on bars/text
LB_GOLD     = (200, 148, 10)  # sourced from blood_gold.png's fill color
RANK_COLORS = {0: GOLD, 1: SILVER, 2: BRONZE}

# blood drop assets: red=global first blood, purple=team blood, gold=team blood on an active seasonal machine
BLOOD_GLOBAL = "blood_red.png"
BLOOD_TEAM   = "blood_purple.png"
BLOOD_SEASON = "blood_gold.png"

IMG_W, IMG_H   = 800, 200
AVATAR_SIZE    = 140
FRAME_SIZE     = AVATAR_SIZE + 20          # 160x160
FRAME_POS      = (12, (IMG_H - FRAME_SIZE) // 2)  # vertically centered in the card
AVATAR_POS     = (FRAME_POS[0] + (FRAME_SIZE - AVATAR_SIZE) // 2,
                  FRAME_POS[1] + (FRAME_SIZE - AVATAR_SIZE) // 2)  # centered in frame
MACHINE_SIZE   = 128
MACHINE_POS    = (660, 59)  # intentionally kept low in the card, unlike the user avatar
ICON_SIZE      = 55
BLOOD_SIZE     = 45

# bottom of machine area
ICONS_Y = MACHINE_POS[1] + MACHINE_SIZE - ICON_SIZE + 3

# (size, max_chars) tiers for the body line, largest-fits-first. max_chars is
# how many characters fit before the box avatar at x=660; text past the
# smallest tier gets truncated with an ellipsis.
BODY_TEXT_FONT_TIERS = [
    (30, 27),
    (24, 34),
    (18, 46),
]


def _load_blood(filename):
    """Loads a blood-drop asset and scales it to BLOOD_SIZE tall, preserving
    aspect ratio. Returns None if the file is missing or unreadable.
    """
    try:
        img = Image.open(ASSETS / filename).convert("RGBA")
        h = BLOOD_SIZE
        w = int(h * img.width / img.height)
        return img.resize((w, h), Image.LANCZOS)
    except Exception:
        return None


def _load_img(path, size):
    """Loads an image file and resizes it to size x size. Returns None if
    `path` is falsy or the file can't be loaded, so callers can treat a
    missing/broken image the same as no image at all.
    """
    if not path:
        return None
    try:
        img = Image.open(path).convert("RGBA")
        return img.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def _circle_crop(img):
    """Masks a square image into a circle, returning a new RGBA image of the
    same size with everything outside the circle made transparent.
    """
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, mask=mask)
    return out


def _fit_body_text(text):
    """Picks the largest size from BODY_TEXT_FONT_TIERS that fits `text`
    (returns it unchanged), or truncates it with an ellipsis at the smallest
    tier's size if even that doesn't fit.
    """
    smallest_size, smallest_max = BODY_TEXT_FONT_TIERS[-1]
    for size, max_chars in BODY_TEXT_FONT_TIERS:
        if len(text) <= max_chars:
            return text, size
    return text[:smallest_max - 1] + "…", smallest_size


def generate_solve_image(entry, user_avatar_path, machine_avatar_path=None,
                          discord_display_name=None, tag=None):
    """Renders a pwn-alert card (PNG, returned as a BytesIO) for one solve:
    user avatar, username, what was solved, the box/challenge avatar, a
    user/root icon for machines, a blood-drop indicator, and optionally a
    linked Discord handle and/or profile tag.
    """
    own_type = entry["type"]        # "user" | "root" | "challenge"
    obj_type = entry["object_type"] # "machine" | "challenge"
    username = entry["user_name"]
    obj_name = entry["object_name"]
    blood    = entry["first_blood"]
    category = entry.get("category")

    if obj_type == "challenge":
        body_text = f"Just solved {obj_name}"
    elif own_type == "user":
        body_text = f"Just got user on {obj_name}"
    elif own_type == "root":
        body_text = f"Just got root on {obj_name}"
    else:
        body_text = f"Just solved {obj_name}"

    body_text, body_font_size = _fit_body_text(body_text)

    img  = Image.new("RGBA", (IMG_W, IMG_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    try:
        font_bold   = ImageFont.truetype(str(ASSETS / "UbuntuMono-Bold.ttf"), 50)
        font_body   = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), body_font_size)
        font_handle = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), 22)
        font_tag    = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), 15)
    except Exception:
        font_bold = font_body = font_handle = font_tag = ImageFont.load_default()

    # user avatar (circular) -- fall back to HTB logo for users with no avatar set
    _paste_avatar_with_frame(img, user_avatar_path)

    # machine / challenge avatar
    obj_img = _load_img(machine_avatar_path, MACHINE_SIZE)
    if obj_img:
        img.paste(obj_img, MACHINE_POS, obj_img)

    # user / root icon (bottom-right of machine area) -- anchored to the box
    # avatar, so skip it if that avatar failed to load (e.g. the remote host
    # returned an error), otherwise it would float alone against the plain
    # background with nothing to anchor to.
    is_machine = (obj_type == "machine")
    if is_machine and obj_img:
        icon_file = "user.png" if own_type == "user" else "root.png"
        icon = _load_img(str(ASSETS / icon_file), ICON_SIZE)
        if icon:
            icon_x = MACHINE_POS[0] + MACHINE_SIZE - ICON_SIZE
            img.paste(icon, (icon_x, ICONS_Y), icon)

    # blood drop (bottom-left of machine area) -- same reasoning as the icon
    # above, only draw it alongside a real box avatar.
    # global bloods are always also team bloods, but only global bloods show red --
    # gold is reserved for team bloods on the active seasonal machines.
    team_blood       = entry.get("team_blood", False)
    is_season_blood  = entry.get("is_season_machine", False)
    if blood:
        blood_file = BLOOD_GLOBAL
    elif team_blood and is_season_blood:
        blood_file = BLOOD_SEASON
    elif team_blood:
        blood_file = BLOOD_TEAM
    else:
        blood_file = None
    if blood_file and obj_img:
        blood_img = _load_blood(blood_file)
        if blood_img:
            img.paste(blood_img, (MACHINE_POS[0], ICONS_Y), blood_img)

    draw.text((228, 46),  username,  font=font_bold, fill=WHITE)
    draw.text((228, 110), body_text, font=font_body, fill=WHITE)

    # tag sits under the username/body column -- independent of whether the
    # Discord handle below is showing, since a user can have a tag on file even
    # if their Discord member object can't currently be resolved (e.g. they left
    # the guild). MAX_TAG_LENGTH keeps this clear of the box avatar at x=660+.
    if tag:
        draw.text((228, 152), tag[:MAX_TAG_LENGTH], font=font_tag, fill=DIM_WHITE)

    # linked Discord identity -- shown whenever this user is claimed, independent
    # of avatar_pref (that only controls which avatar image is used). Right-aligned
    # above the box avatar so a long display name grows leftward instead of
    # overflowing off the right edge of the card.
    if discord_display_name:
        handle_text = f"@{discord_display_name}"
        bbox = draw.textbbox((0, 0), handle_text, font=font_handle)
        text_w = bbox[2] - bbox[0]
        right_edge = MACHINE_POS[0] + MACHINE_SIZE
        draw.text((right_edge - text_w, 24), handle_text, font=font_handle, fill=HTB_GREEN)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


def _draw_progress_bar(img, x, y, width, height, pct):
    """Draws a horizontal progress bar: a background track plus a filled
    portion proportional to `pct` (0-100, clamped).
    """
    draw = ImageDraw.Draw(img)
    draw.rectangle([x, y, x + width, y + height], fill=BAR_BG)
    fill_w = int(width * min(max(pct / 100.0, 0.0), 1.0))
    if fill_w > 0:
        draw.rectangle([x, y, x + fill_w, y + height], fill=HTB_GREEN)


def _paste_avatar_with_frame(img, avatar_path):
    """Pastes a circular avatar (falling back to the default HTB logo if
    `avatar_path` is missing/unreadable) plus its decorative frame overlay,
    at the layout's fixed avatar position.
    """
    avatar = (_load_img(avatar_path, AVATAR_SIZE)
              or _load_img(str(ASSETS / "user_default.png"), AVATAR_SIZE))
    if avatar:
        img.paste(_circle_crop(avatar), AVATAR_POS, _circle_crop(avatar))
    try:
        frame = Image.open(ASSETS / "frame.png").convert("RGBA")
        frame = frame.resize((FRAME_SIZE, FRAME_SIZE), Image.LANCZOS)
        img.paste(frame, FRAME_POS, frame)
    except Exception:
        pass


LB_HEADER_H  = 90
LB_ROW_H     = 52
LB_FOOTER_H  = 36
LB_AVATAR_SZ = 38


def _load_avatar_circle(path, size):
    """Loads an avatar and circle-crops it, falling back to the default HTB
    logo if `path` is missing/unreadable.
    """
    avatar = _load_img(path, size) or _load_img(str(ASSETS / "user_default.png"), size)
    return _circle_crop(avatar)


def generate_leaderboard_image(title, subtitle, rows, accent, value_suffix,
                                icon_path=None, team_name="GANGGANG") -> io.BytesIO:
    """Renders a leaderboard (PNG, returned as a BytesIO): a title/subtitle
    header, one ranked row per entry in `rows` (rank, avatar, name, a value
    bar scaled to the highest value, and the value itself), and a footer with
    the team name. `rows` is a list of {"name", "value", "avatar_path"} dicts,
    already in rank order.
    """
    img_h = LB_HEADER_H + LB_ROW_H * len(rows) + LB_FOOTER_H
    img  = Image.new("RGBA", (IMG_W, img_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    try:
        f_title = ImageFont.truetype(str(ASSETS / "UbuntuMono-Bold.ttf"), 34)
        f_sub   = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), 18)
        f_rank  = ImageFont.truetype(str(ASSETS / "UbuntuMono-Bold.ttf"), 22)
        f_name  = ImageFont.truetype(str(ASSETS / "UbuntuMono-Bold.ttf"), 22)
        f_val   = ImageFont.truetype(str(ASSETS / "UbuntuMono-Bold.ttf"), 22)
        f_foot  = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), 14)
    except Exception:
        f_title = f_sub = f_rank = f_name = f_val = f_foot = ImageFont.load_default()

    draw.text((24, 20), title, font=f_title, fill=accent)
    draw.text((24, 60), subtitle, font=f_sub, fill=DIM_WHITE)
    draw.rectangle([0, LB_HEADER_H - 1, IMG_W, LB_HEADER_H + 1], fill=SEPARATOR)

    max_val = max((r["value"] for r in rows), default=1) or 1

    y = LB_HEADER_H
    for i, row in enumerate(rows):
        if i % 2 == 1:
            draw.rectangle([0, y, IMG_W, y + LB_ROW_H], fill=ROW_ALT)

        cy = y + LB_ROW_H // 2

        rank_color = RANK_COLORS.get(i, DIM_WHITE)
        draw.text((28, cy - 12), str(i + 1), font=f_rank, fill=rank_color)

        avatar = _load_avatar_circle(row.get("avatar_path"), LB_AVATAR_SZ)
        img.paste(avatar, (70, cy - LB_AVATAR_SZ // 2), avatar)

        draw.text((124, cy - 12), row["name"], font=f_name, fill=WHITE)

        bar_x, bar_w, bar_h = 430, 190, 8
        bar_y = cy - bar_h // 2
        draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                       fill=ROW_ALT if i % 2 == 0 else BG_COLOR)
        fill_w = int(bar_w * (row["value"] / max_val))
        if fill_w > 0:
            draw.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], fill=accent)

        val_text = f"{row['value']} {value_suffix}"
        bbox = draw.textbbox((0, 0), val_text, font=f_val)
        tw = bbox[2] - bbox[0]
        draw.text((IMG_W - 30 - tw, cy - 12), val_text, font=f_val, fill=accent)

        if icon_path:
            try:
                icon = Image.open(icon_path).convert("RGBA")
                ih = 22
                iw = int(ih * icon.width / icon.height)
                icon = icon.resize((iw, ih), Image.LANCZOS)
                img.paste(icon, (IMG_W - 30 - tw - iw - 8, cy - ih // 2), icon)
            except Exception:
                pass

        y += LB_ROW_H

    draw.rectangle([0, y, IMG_W, y + 1], fill=SEPARATOR)
    draw.text((24, y + 10), f"Team: {team_name}", font=f_foot, fill=DIM_WHITE)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


# Shared by both the pwn-alert card and the !stats card -- the pwn card is the
# tighter constraint of the two, so it sets the limit for both. There, the tag
# sits at x=228 and must clear the box avatar at x=660 (MACHINE_POS), leaving a
# 432px column; at 15pt UbuntuMono-Regular (genuinely monospaced, 7.5px/char)
# minus a 16px safety margin, that's floor(416 / 7.5) = 55 characters -- verified
# directly against the font, so a tag at this length physically cannot reach the
# box avatar. The !stats card has more room to spare (75 would fit there), but
# using one shared limit keeps a tag's length meaningful everywhere it's shown.
MAX_TAG_LENGTH = 55
_TAG_FONT_SIZE = 15
_TAG_ROW_X     = 195
_TAG_ROW_Y     = 145


def generate_stats_image(user, stats, avatar_path, discord_display_name=None, tag=None) -> io.BytesIO:
    """Renders a !stats card (PNG, returned as a BytesIO): avatar, name, rank,
    points, optional linked Discord handle/tag, then machine/challenge/
    sherlock stat columns, and (if any) pro lab / fortress progress rows.
    """
    n_prolabs    = len(stats["prolabs"])
    n_fortresses = len(stats["fortresses"])
    has_extras   = n_prolabs + n_fortresses > 0

    n_sections = (1 if n_prolabs > 0 else 0) + (1 if n_fortresses > 0 else 0)
    img_h = 350 + (16 if has_extras else 0) + n_sections * 22 + (n_prolabs + n_fortresses) * 42 + 10
    img  = Image.new("RGBA", (800, img_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    try:
        f_title = ImageFont.truetype(str(ASSETS / "UbuntuMono-Bold.ttf"),
                                     38 if len(user["name"]) <= 14 else 26)
        f_label = ImageFont.truetype(str(ASSETS / "UbuntuMono-Bold.ttf"), 15)
        f_body  = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), 18)
        f_sub   = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), 20)
    except Exception:
        f_title = f_label = f_body = f_sub = ImageFont.load_default()

    # avatar + frame
    _paste_avatar_with_frame(img, avatar_path)

    # header text
    draw.text((195, 28),  user["name"],                          font=f_title, fill=WHITE)
    draw.text((195, 85),  user.get("rank") or "Noob",            font=f_sub,   fill=HTB_GREEN)
    draw.text((195, 118), f"{user.get('points') or 0} pts",      font=f_sub,   fill=WHITE)

    # linked Discord identity -- shown whenever this profile is claimed, independent
    # of avatar_pref (that only controls which avatar image is used). Discord's own
    # display name/nickname cap (32 chars) always fits top-right at f_sub (20pt),
    # so no per-string resizing is needed there.
    if discord_display_name:
        handle_text = f"@{discord_display_name}"
        bbox = draw.textbbox((0, 0), handle_text, font=f_sub)
        draw.text((790 - (bbox[2] - bbox[0]), 24), handle_text, font=f_sub, fill=HTB_GREEN)

        # Tag gets its own full-width row in the existing gap between the header
        # block and the separator -- one fixed size (see MAX_TAG_LENGTH) rather than
        # per-string resizing, since it comfortably fits the intended use case
        # (a GitHub Pages / blog URL) without needing to shrink at all.
        if tag:
            f_tag = ImageFont.truetype(str(ASSETS / "UbuntuMono-Regular.ttf"), _TAG_FONT_SIZE)
            draw.text((_TAG_ROW_X, _TAG_ROW_Y), tag[:MAX_TAG_LENGTH], font=f_tag, fill=DIM_WHITE)

    # separator
    draw.rectangle([10, 175, 790, 177], fill=SEPARATOR)

    # stats columns
    C1, C2, C3 = 20, 290, 555
    sy = 191
    rh = 28

    draw.text((C1, sy),       "MACHINES",                                     font=f_label, fill=HTB_GREEN)
    draw.text((C1, sy+rh),    f"User:    {stats['machine_user']}",            font=f_body,  fill=WHITE)
    draw.text((C1, sy+rh*2),  f"Root:    {stats['machine_root']}",            font=f_body,  fill=WHITE)
    draw.text((C1, sy+rh*3),  f"Global:  {stats['machine_bloods']}",          font=f_body,  fill=GLOBAL_RED)
    draw.text((C1, sy+rh*4),  f"Team:    {stats['machine_team_bloods']}",     font=f_body,  fill=TEAM_PURPLE)

    draw.text((C2, sy),       "CHALLENGES",                                   font=f_label, fill=HTB_GREEN)
    draw.text((C2, sy+rh),    f"Solved:  {stats['challenges']}",              font=f_body,  fill=WHITE)
    draw.text((C2, sy+rh*2),  f"Global:  {stats['challenge_bloods']}",        font=f_body,  fill=GLOBAL_RED)
    draw.text((C2, sy+rh*3),  f"Team:    {stats['challenge_team_bloods']}",   font=f_body,  fill=TEAM_PURPLE)

    draw.text((C3, sy),       "SHERLOCKS",                                    font=f_label, fill=HTB_GREEN)
    draw.text((C3, sy+rh),    f"Solved:  {stats['sherlocks']}",               font=f_body,  fill=WHITE)
    draw.text((C3, sy+rh*2),  f"Global:  {stats['sherlock_bloods']}",         font=f_body,  fill=GLOBAL_RED)
    draw.text((C3, sy+rh*3),  f"Team:    {stats['sherlock_team_bloods']}",    font=f_body,  fill=TEAM_PURPLE)

    y = sy + rh * 5 + 10

    if has_extras:
        draw.rectangle([10, y, 790, y + 2], fill=SEPARATOR)
        y += 14

        for section, items, id_key, total_key, owned_key in [
            ("PRO LABS",   stats["prolabs"],    "name", "total_flags", "owned_flags"),
            ("FORTRESSES", stats["fortresses"], "name", "total_flags", "owned_flags"),
        ]:
            if not items:
                continue
            draw.text((20, y), section, font=f_label, fill=HTB_GREEN)
            y += 22
            for item in items:
                pct   = item.get("completion_percentage") or 0
                total = item[total_key]
                owned = item[owned_key]
                draw.text((20, y + 3), item["name"][:18], font=f_body, fill=WHITE)
                _draw_progress_bar(img, 215, y + 7, 300, 13, pct)
                draw.text((525, y + 3), f"{owned}/{total} flags", font=f_body, fill=WHITE)
                y += 42

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
