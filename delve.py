#!/usr/bin/env python3
"""
DELVE - a co-op dungeon crawler for a person and their AI (or up to four of you).  v2.
Light on story, heavy on fights. The SCRIPT is the dungeon: it rolls every die and plays every monster, trap and chest.
Each of you owns ONE hero and makes that hero's choices; the call on where to go ALTERNATES between you.

  delve.py new [--mode maze|doors] [--diff story|easy|normal|hard|brutal|nightmare]
  delve.py hire <HeroName> <fighter|paladin|thief|priest|mage> <WhoPlaysIt>     (2-4 heroes; foes scale with the party)
  delve.py reroll <Name>      one reroll each, before the first descent
  delve.py guide              one-screen refresher (start here after a break)        delve.py diff [level]   delve.py records
  delve.py board              EVERYTHING you need right now          delve.py party | sheet [Name] | log [n] | info <thing>
  delve.py go                 enter the dungeon (from town)

 EXPLORING (maze)                                     EXPLORING (doors)
  delve.py map [--img]   the map, with A-J across, 1-10 down     delve.py doors      three doors, a hint on each
  delve.py goto C4       walk to a square (explored, or the       delve.py pick 1|2|3
                         unexplored one beside an opening)
  delve.py stairs | camp (rest - risky) | home (doors: after a guardian) | use <Name> potion [ally] | cast <Name> <spell> [ally]

 FIGHTING (each owner orders their own hero; the round resolves when every order is in)
  delve.py do <Name> attack [group] | cast <spell> [group|ally] | defend | potion [ally]
  delve.py stance <Name> auto|attack|defend|off   standing order FOR THIS FIGHT (auto = attack until anyone is below half)
  delve.py run                the whole party tries to flee
  COMBOS: put foes to SLEEP or CHILL them with frost, and the next weapon hit on one does half again as much.

 LOOT    a find worth having is offered to everyone:  delve.py loot need <Name> | loot pass <Name>   (two needs = dice)
 TALENTS at levels 2 and 4 each hero picks one of two:  delve.py learn <Name> <talent>
 CHESTS  delve.py chest inspect <Name> | disarm <Name> | open | leave          SHRINES  delve.py shrine pray <Name> | leave
 TOWN    delve.py town | inn | shop | want <Name> <item> | unwant <Name> <item> | cart | agree <Name> | sell <item> | equip <Name> <item>
         (you shop TOGETHER: things go in a cart, and nothing is bought until each of you agrees)

Pictures come automatically at each fight round, new floor and set of doors.
"""
import json, os, re, sys, random, time, zlib, struct, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
SAVE = os.path.join(HERE, "state.json")
OUT = os.path.join(HERE, "out")
LOG = os.path.join(HERE, "log.md")

# ---- this install's own settings (delve_local.json beside the script; see delve_local.example.json). Without the
# file the game is plain: nobody is waited for (honor system) and pictures are only written to out/.
def _local():
    try: return json.load(open(os.path.join(HERE, "delve_local.json")))
    except Exception: return {}
CFG = _local()
HUMANS = CFG.get("humans", [])            # owners the script WAITS for (a real person's reply)
GUARD = CFG.get("guard", "none")          # how the script knows a human has spoken: "none" | "file" | "openclaw"
SENDERS = CFG.get("senders", {})          # how each human's messages are labelled in that session
TG_CHAT = (CFG.get("send") or {}).get("telegram_chat", "")

def die(m): print(m); sys.exit(1)
def d(n, s): return sum(random.randint(1, s) for _ in range(n))
def dice(expr):
    m = re.fullmatch(r"(\d+)d(\d+)([+-]\d+)?", expr.replace(" ", ""))
    return d(int(m.group(1)), int(m.group(2))) + int(m.group(3) or 0) if m else int(expr)
def bonus(x): return (x - 10) // 3

def load():
    if not os.path.exists(SAVE): die("No expedition. delve.py new")
    g = json.load(open(SAVE)); ch = False
    if "diff" not in g: g["diff"] = "normal"; ch = True
    for c in g.get("party", []):
        for k, v in (("tal", []), ("waff", ""), ("aaff", ""), ("charm", "")):
            if k not in c: c[k] = v; ch = True
    if "learn_opened" not in g and any(pending_picks(c) for c in g.get("party", [])): g["learn_opened"] = now_iso(); ch = True
    if g.get("floor") == 0 and "town_at" not in g: g["town_at"] = now_iso(); ch = True
    if ch: save(g)
    return g

def save(g):
    tmp = SAVE + ".tmp"; json.dump(g, open(tmp, "w")); os.replace(tmp, SAVE)
REC = os.path.join(HERE, "saves", "records.json")
def record(g, key, n=1):
    """Lifetime tallies per difficulty (saves/records.json): deepest floor, kills, guardians, wipes."""
    try:
        os.makedirs(os.path.dirname(REC), exist_ok=True)
        try: r = json.load(open(REC))
        except Exception: r = {}
        dd = r.setdefault(g.get("diff", "normal"), {})
        if key == "deepest": dd["deepest"] = max(dd.get("deepest", 0), n)
        else: dd[key] = dd.get(key, 0) + n
        json.dump(r, open(REC, "w"), indent=1)
    except Exception: pass
def price(g, base): return max(1, int(round(base * DF(g)["price"])))

def log(g, line):
    g.setdefault("tail", []).append(line); g["tail"] = g["tail"][-40:]
    try:
        with open(LOG, "a") as f: f.write("- %s  %s\n" % (time.strftime("%m-%d %H:%M"), line))
    except Exception: pass

def pc(g, name):
    for c in g["party"]:
        if c["name"].lower() == name.lower(): return c
    for c in g["party"]:
        if c["name"].lower().startswith(name.lower()): return c
    die("No one called '%s'. Party: %s" % (name, ", ".join(c["name"] for c in g["party"])))

def alive(g): return [c for c in g["party"] if c["hp"] > 0 and "dead" not in c["st"]]
def can_act(c): return c["hp"] > 0 and not any(s in c["st"] for s in ("dead", "asleep", "paralyzed"))

# WHY THE SCRIPT CHECKS: in play-testing the AI kept ordering the human's hero for them ("your order, tank..." and then
# typing it itself, six rounds running). Prompts didn't hold it, so the script does: a human's hero - and a human's
# calls - only move after that human has actually written since the decision came up.
#   guard "file":     whatever runs your AI touches  human_said/<Name>  each time that person sends a message
#                     (a hook, a wrapper script, or the person running `delve.py said <Name>` themselves).
#   guard "openclaw": read the agent's OpenClaw session files for that person's real messages.
#   guard "none":     honor system.
SESS = os.path.expanduser("~/.openclaw/agents/main/sessions")
def now_iso(): return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
def last_word_from(owner):
    """UTC ISO time of this owner's most recent real message. "9999" = not checked (not a human here / no guard)."""
    if owner not in HUMANS or GUARD not in ("openclaw", "file") or os.environ.get("DELVE_NOGUARD"): return "9999"
    if GUARD == "file":
        try: return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(os.path.getmtime(os.path.join(HERE, "human_said", owner))))
        except Exception: return "0000"      # never spoken yet
    tag = '"sender": "%s' % SENDERS.get(owner, owner)
    try:
        import glob as _g
        fs = sorted([f for f in _g.glob(os.path.join(SESS, "*.jsonl")) if ".trajectory" not in f], key=os.path.getmtime)
        for f in reversed(fs[-2:]):
            for line in reversed(open(f, errors="replace").read().splitlines()[-400:]):
                try: d_ = json.loads(line)
                except Exception: continue
                m = d_.get("message") or {}
                if d_.get("type") != "message" or m.get("role") != "user": continue
                t = " ".join(b.get("text", "") for b in m.get("content", []) if isinstance(b, dict))
                if tag not in t: continue
                body = t.split("```")[-1].strip()
                if body.startswith(tuple(CFG.get("not_human_prefixes", []) or ["\x00"])) or any(s in body for s in CFG.get("not_human_marks", [])): continue
                return d_.get("timestamp", "")[:19]
    except Exception:
        return "9999"                      # can't read the session: don't block the game
    return ""

def needs_word(g, owner, since_key, what):
    since = g.get(since_key) or ""
    said = last_word_from(owner)
    if owner in HUMANS and said and said <= since:
        die("Hold on - that's %s's %s, and %s hasn't said anything since it came up. Ask and wait for the answer." % (owner, what, owner))

# ONE BEAT PER MESSAGE. In play-testing, with the human's hero on a standing order, the AI played two whole fights, a
# chest, a door and a shop visit (15 commands) on one message from them. So a finished fight or a find closes the beat:
# nothing moves the expedition on until every human has written since. A standing order lasts one fight (start_fight).
def set_pause(g): g["pause"] = now_iso()
def needs_turn(g):
    since = g.get("pause") or ""
    if not since: return
    for h in HUMANS:
        if not any(c["owner"] == h for c in g["party"]): continue
        said = last_word_from(h)
        if said and said <= since:
            die("That's this beat done - %s hasn't seen what just happened yet. Tell them in a line or two (what happened, "
                "the numbers that matter, what you'd like to do next), END your message and wait. The game moves on when %s answers. "
                "(Looking is always fine: board, sheet, info, guide.)" % (h, h))

def next_call(g):
    owners = sorted(set(c["owner"] for c in g["party"]))
    if len(owners) < 2: return owners[0] if owners else "?"
    g["leader"] = owners[(owners.index(g.get("leader", owners[0])) + 1) % len(owners)] if g.get("leader") in owners else owners[0]
    g["call_opened"] = now_iso()
    return g["leader"]

# ---------------------------------------------------------------- classes ---
CLS = {   # one hero each - that's the roleplay element
 "fighter": {"hd": 12, "atk": 3, "need": {"str": 12}, "mp": None,  "wpn": "sword",  "arm": "mail",    "blurb": "toughest, hits hardest, two swings from level 5"},
 "paladin": {"hd": 10, "atk": 2, "need": {"str": 11, "pie": 10}, "mp": "pie", "wpn": "mace", "arm": "mail", "blurb": "armored fighter with a little healing (heal, ward)"},
 "thief":   {"hd": 9,  "atk": 2, "need": {"agi": 12}, "mp": None,  "wpn": "dagger", "arm": "leather", "blurb": "crits often, disarms chests, strikes first"},
 "priest":  {"hd": 8,  "atk": 1, "need": {"pie": 12}, "mp": "pie", "wpn": "mace",   "arm": "mail",    "blurb": "heals, wards, banishes undead, raises the fallen"},
 "mage":    {"hd": 8,  "atk": 0, "need": {"int": 12}, "mp": "int", "wpn": "staff",  "arm": "robe",    "blurb": "sleep, fire and frost - hits whole groups"},
}
WEAPONS = {"dagger": (1, 4), "staff": (1, 6), "mace": (1, 6), "sword": (1, 8), "axe": (1, 10), "greatsword": (2, 6)}
ARMOR = {"robe": 0, "leather": 2, "mail": 4, "plate": 6}
SPELLS = {   # name: ({class: level learned}, mp, kind, power)
 "spark":  ({"mage": 1}, 1, "hit1", "1d8"),      "sleep": ({"mage": 1}, 2, "sleep", None),
 "flame":  ({"mage": 3}, 3, "hitgroup", "2d6"),  "frost": ({"mage": 5}, 4, "hitgroup", "3d8"), "storm": ({"mage": 7}, 6, "hitall", "4d6"),
 "heal":   ({"priest": 1, "paladin": 2}, 1, "heal1", "1d8"), "ward": ({"priest": 1, "paladin": 3}, 1, "ward", None),
 "cure":   ({"priest": 3, "paladin": 5}, 1, "cure", None),   "banish": ({"priest": 3, "paladin": 6}, 2, "banish", None),
 "mend":   ({"priest": 5}, 4, "healall", "1d8"), "raise": ({"priest": 7}, 8, "raise", None),
}
XP_AT = [0, 100, 260, 520, 950, 1600, 2600, 4000, 6000, 8800, 12500, 17500]
STATS = ["str", "agi", "vit", "int", "pie", "luk"]

# ------------------------------------------------------------------ talents ---
# At levels 2 and 4 each hero picks ONE of two. That's the build: no two priests need be the same.
TALENTS = {
 "fighter": {2: [("cleave", "a killing blow lets you swing once more"), ("ironskin", "+1 AC, always")],
             4: [("bloodlust", "+3 damage while below half HP"), ("bulwark", "while you DEFEND, foes mostly come at you instead of your friends")]},
 "paladin": {2: [("smite", "+1d6 damage to undead and spellcasters"), ("mercy", "your healing spells heal half again as much")],
             4: [("aura", "everyone gets +1 AC while you stand"), ("zeal", "+1 to hit and +1 damage")]},
 "thief":   {2: [("backstab", "your first hit in every fight is a critical"), ("pockets", "+25% gold from every fight")],
             4: [("venom", "your hits poison: 2 damage a round to that foe"), ("evasion", "half damage from breath, blasts and pits")]},
 "priest":  {2: [("blessing", "+3 to every heal"), ("censure", "your ward gives +3 AC instead of +2")],
             4: [("sanctuary", "your heals also end poison, sleep and paralysis"), ("fount", "+4 max MP")]},
 "mage":    {2: [("focus", "+2 damage on every spell hit"), ("deepsleep", "sleep lands more often and holds longer")],
             4: [("manaflow", "+1 MP back every round of a fight"), ("chain", "spark jumps to a second foe")]},
}
def has(c, tal): return tal in c.get("tal", [])
def charm(c, n): return c.get("charm") == n
def pending_picks(c):
    """Levels at which this hero still owes a talent choice."""
    mine = c.get("tal", []); out = []
    for L, pair in TALENTS.get(c["cls"], {}).items():
        if c["lvl"] >= L and not any(k in mine for k, _ in pair): out.append(L)
    return out
def pick_line(c):
    out = []
    for L in pending_picks(c):
        (a, ad), (b, bd) = TALENTS[c["cls"]][L]
        out.append("CHOOSE (level %d talent) for %s:  learn %s %s  = %s   |   learn %s %s  = %s" % (L, c["name"], c["name"], a, ad, c["name"], b, bd))
    return out

# -------------------------------------------------------------------- items ---
# Gear is a name: "[affix] base [+N]" (weapon or armor) or a charm. One weapon, one armor, one charm per hero.
WAFF = {"keen": "crits on 19-20", "vampiric": "heals you 2 HP on every hit", "flaming": "+1d4 fire damage", "heavy": "+3 damage but -1 to hit",
        "swift": "you act earlier in the round", "holy": "+1d6 to undead"}
AAFF = {"warded": "-2 damage from spells and breath", "thorned": "whoever hits you takes 2", "blessed": "you can't be poisoned", "shadowed": "foes pick you less often"}
CHARMS = {"ring of mending": "+1 HP at the end of every round", "lucky coin": "+20% gold from fights, for the party", "amulet of clarity": "+4 max MP",
          "boots of flight": "the party can always flee (not from a guardian)", "wolf tooth": "+1 to hit and +1 damage",
          "saint's knuckle": "once a fight, a blow that would drop you leaves you at 1 HP", "ember stone": "+2 damage on every spell hit"}
def item_parse(name):
    """-> (slot, base, plus, affix) or None"""
    n = re.sub(r"\s+", " ", (name or "").lower().strip())
    if n in CHARMS: return ("charm", n, 0, "")
    m = re.fullmatch(r"(?:(\w+) )?(\w+)(?: \+(\d+))?", n)
    if not m: return None
    aff, base, plus = m.group(1) or "", m.group(2), int(m.group(3) or 0)
    if base in WEAPONS and (not aff or aff in WAFF): return ("weapon", base, plus, aff)
    if base in ARMOR and (not aff or aff in AAFF): return ("armor", base, plus, aff)
    return None
def item_name(base, plus=0, aff=""): return ("%s " % aff if aff else "") + base + (" +%d" % plus if plus else "")
def item_desc(name):
    p = item_parse(name)
    if not p: return name
    slot, base, plus, aff = p
    if slot == "charm": return "%s (charm: %s)" % (base, CHARMS[base])
    if slot == "weapon": return "%s (weapon %dd%d%s%s)" % (name, WEAPONS[base][0], WEAPONS[base][1], "+%d" % plus if plus else "", (", " + WAFF[aff]) if aff else "")
    return "%s (armor AC +%d%s)" % (name, ARMOR[base] + plus, (", " + AAFF[aff]) if aff else "")
def can_use(c, name):
    """'' if this hero can use it, else why not."""
    p = item_parse(name)
    if not p: return "that isn't gear"
    slot, base, plus, aff = p
    if slot == "weapon" and c["cls"] == "mage" and base not in ("staff", "dagger"): return "mages use a staff or a dagger"
    if slot == "armor" and c["cls"] == "mage" and base != "robe": return "mages can't cast in armor heavier than a robe"
    if slot == "armor" and c["cls"] == "thief" and base == "plate": return "a thief in plate? no"
    return ""
def worn(c, slot):
    if slot == "weapon": return item_name(c["wpn"], c["wplus"], c.get("waff", ""))
    if slot == "armor": return item_name(c["arm"], c["aplus"], c.get("aaff", ""))
    return c.get("charm") or ""
def put_on(g, c, name):
    """Equip; whatever was worn in that slot goes to the shared bag."""
    slot, base, plus, aff = item_parse(name); old = worn(c, slot)
    if slot == "weapon": c["wpn"], c["wplus"], c["waff"] = base, plus, aff
    elif slot == "armor": c["arm"], c["aplus"], c["aaff"] = base, plus, aff
    else: c["charm"] = base
    if old: g.setdefault("bag", []).append(old)
    c["maxmp"] = mp_for(c); c["mp"] = min(c["mp"], c["maxmp"])
def gen_item(g, good=False):
    """A piece of gear worth arguing over. Deeper floors roll higher pluses and more affixes."""
    fl = max(1, g.get("floor", 1)); r = random.random()
    if r < .25: return random.choice(list(CHARMS))
    plus = (1 if random.random() < .35 + .08 * fl else 0) + (1 if fl >= 4 and random.random() < .4 else 0) + (1 if fl >= 7 and random.random() < .4 else 0)
    aff_ch = .30 + .06 * fl + (.3 if good else 0)
    if r < .65:
        base = random.choices(list(WEAPONS), [3, 3, 3, 4, 3, 2])[0]
        aff = random.choice(list(WAFF)) if random.random() < aff_ch else ""
    else:
        base = random.choices(["robe", "leather", "mail", "plate"], [2, 4, 4, 1 + (fl >= 3)])[0]
        aff = random.choice(list(AAFF)) if random.random() < aff_ch else ""
    if not plus and not aff: plus = 1
    return item_name(base, plus, aff)
def item_value(name):
    p = item_parse(name)
    if not p: return 5
    slot, base, plus, aff = p
    if slot == "charm": return 90
    return int((SHOP.get(base, 25)) * (1 + .6 * plus + (.8 if aff else 0)))

def roll_char(name, cls, owner):
    cl = CLS[cls]
    st = {s: d(3, 6) for s in STATS}
    for k, v in cl["need"].items(): st[k] = max(st[k], v + random.randint(0, 4))
    spare = random.choice([3, 4, 5, 6, 7, 8, 10, 12, 16]) if random.random() < .9 else random.randint(17, 25)   # Wizardry-style bonus roll
    key = list(cl["need"])[0]
    order = [key, "vit", "agi"] + [s for s in STATS if s not in (key, "vit", "agi")]
    for i in range(spare): st[order[i % 3]] += 1 if st[order[i % 3]] < 18 else 0
    hp = cl["hd"] + 6 + max(0, bonus(st["vit"])) + random.randint(0, 3)
    c = {"name": name, "cls": cls, "owner": owner, "lvl": 1, "xp": 0, "s": st, "hp": hp, "maxhp": hp, "st": [],
         "wpn": cl["wpn"], "wplus": 0, "arm": cl["arm"], "aplus": 0, "rerolled": False, "kills": 0, "tal": [], "waff": "", "aaff": "", "charm": ""}
    c["maxmp"] = mp_for(c); c["mp"] = c["maxmp"]
    return c

def mp_for(c):
    k = CLS[c["cls"]]["mp"]
    if not k: return 0
    extra = (4 if has(c, "fount") else 0) + (4 if charm(c, "amulet of clarity") else 0)
    if c["cls"] == "paladin": return 0 if c["lvl"] < 2 else 1 + c["lvl"] + max(0, bonus(c["s"][k])) + extra
    return 4 + c["lvl"] * 2 + max(0, bonus(c["s"][k])) + extra
def known(c): return [n for n, sp in SPELLS.items() if sp[0].get(c["cls"], 99) <= c["lvl"]]
def ac(c, g=None):
    a = 11 + ARMOR[c["arm"]] + c["aplus"] + max(-2, bonus(c["s"]["agi"]))
    if has(c, "ironskin"): a += 1
    if g and any(has(x, "aura") and x["hp"] > 0 for x in g.get("party", [])): a += 1
    if g and g.get("fight") and g["fight"].get("ward"): a += 3 if g["fight"]["ward"] == 3 else 2
    if "defending" in c["st"]: a += 4
    if c.get("curse"): a -= 2
    return a
def atk_bonus(c):
    return (CLS[c["cls"]]["atk"] + c["lvl"] // 2 + bonus(c["s"]["str"]) + c["wplus"] + (1 if has(c, "zeal") else 0) + (1 if charm(c, "wolf tooth") else 0)
            - (1 if c.get("waff") == "heavy" else 0) + (1 if c.get("bless") else 0))
def dmg_bonus(c):
    return (max(0, bonus(c["s"]["str"])) + c["wplus"] + c["lvl"] // 3 + (1 if has(c, "zeal") else 0) + (1 if charm(c, "wolf tooth") else 0)
            + (3 if c.get("waff") == "heavy" else 0) + (3 if has(c, "bloodlust") and c["hp"] < c["maxhp"] / 2 else 0) + (1 if c.get("bless") else 0))
def front(g): return [c for c in g["party"] if c["hp"] > 0]

def cline(c, g=None):
    st = ("  " + ",".join(c["st"])) if c["st"] else ""
    mp = (" MP %d/%d" % (c["mp"], c["maxmp"])) if c["maxmp"] else ""
    return "%-9s %-7s L%-2d HP %3d/%-3d%s AC %d  %s%s" % (c["name"], c["cls"], c["lvl"], max(0, c["hp"]), c["maxhp"], mp, ac(c, g), c["owner"], st)

# -------------------------------------------------------------- difficulty ---
# Six settings. NORMAL is the game as first built. HARD and up are meant to hurt: more and tougher foes, elites, a
# guardian that hits harder, dearer potions, and from BRUTAL a fallen hero stays down until someone heals them.
# NIGHTMARE: one wipe ends the expedition for good.
DIFFS = {
 "story":     dict(hp=.70, dmg=.60, hit=-2, more=-1, two=.00, elite=.00, lose=.00, price=.8, heal=1.3, rise=True,  perma=False, amb=.15, blurb="for the company, not the challenge - foes are soft, a wipe costs nothing"),
 "easy":      dict(hp=.85, dmg=.80, hit=-1, more=0,  two=.00, elite=.00, lose=.25, price=1.0, heal=1.1, rise=True,  perma=False, amb=.25, blurb="forgiving - a wipe costs a quarter of the gold"),
 "normal":    dict(hp=1.0, dmg=1.0, hit=0,  more=0,  two=.10, elite=.05, lose=.50, price=1.0, heal=1.0, rise=True,  perma=False, amb=.30, blurb="the game as built - a wipe costs half the gold"),
 "hard":      dict(hp=1.15, dmg=1.1, hit=1, more=1,  two=.25, elite=.15, lose=.75, price=1.4, heal=.8,  rise=True,  perma=False, amb=.40, blurb="more foes, elites, dearer potions - you will go home early some days"),
 "brutal":    dict(hp=1.3, dmg=1.2, hit=2,  more=1,  two=.40, elite=.28, lose=1.0, price=1.7, heal=.65, rise=False, perma=False, amb=.50, blurb="the fallen stay down until healed; a wipe takes every coin and the bag"),
 "nightmare": dict(hp=1.45, dmg=1.3, hit=2, more=2,  two=.50, elite=.40, lose=1.0, price=2.0, heal=.5,  rise=False, perma=True,  amb=.60, blurb="one wipe and the expedition is over. For bragging rights only"),
}
DIFF_ORDER = ["story", "easy", "normal", "hard", "brutal", "nightmare"]
def DF(g): return DIFFS[(g or {}).get("diff", "normal")]
def psize(g): return max(1, len((g or {}).get("party", [])) or 2)

# ---------------------------------------------------------------- monsters ---
MON = {   # name: tier, hp dice, ac, atk, dmg, xp, gold, group size, tags
 "giant rat": (1, "2d4", 11, 1, "1d3", 6, 1, (2, 5), []), "kobold": (1, "2d6", 12, 2, "1d6", 10, 3, (2, 4), []),
 "green slime": (1, "3d6", 9, 1, "1d4", 12, 0, (1, 3), ["poison"]), "cave bat": (1, "1d6", 14, 3, "1d2", 5, 0, (3, 6), []),
 "skeleton": (1, "2d8", 13, 2, "1d6", 15, 4, (1, 4), ["undead"]), "acolyte": (1, "2d6", 11, 1, "1d4", 14, 6, (1, 3), ["caster"]),
 "orc": (2, "3d8", 13, 4, "1d8", 30, 8, (2, 4), []), "zombie": (2, "4d8", 10, 3, "1d8", 28, 2, (1, 4), ["undead"]),
 "giant spider": (2, "3d8", 14, 4, "1d6", 35, 0, (1, 3), ["poison"]), "dark priest": (2, "3d6", 12, 2, "1d6", 40, 15, (1, 2), ["caster"]),
 "wolf": (2, "2d8", 13, 4, "1d6", 25, 0, (2, 5), []), "ogre": (3, "6d8", 14, 6, "2d6", 90, 20, (1, 2), []),
 "ghoul": (3, "4d8", 14, 5, "1d6", 70, 5, (1, 3), ["undead", "paralyze"]), "gargoyle": (3, "5d8", 17, 6, "1d10", 95, 10, (1, 2), []),
 "witch": (3, "4d6", 12, 3, "1d4", 85, 30, (1, 2), ["caster", "sleeper"]), "wraith": (3, "5d8", 15, 5, "1d8", 100, 10, (1, 2), ["undead"]),
 "troll": (4, "8d8", 15, 8, "2d6", 200, 25, (1, 2), ["regen"]), "bone knight": (4, "7d8", 18, 8, "1d12", 220, 40, (1, 3), ["undead"]),
 "fire drake": (4, "10d8", 17, 9, "2d8", 400, 120, (1, 1), ["breath"]), "lich": (5, "12d8", 18, 9, "2d6", 900, 300, (1, 1), ["undead", "caster", "breath"]),
 # the wider bestiary (early floors used to be "2 skeletons" five fights out of ten)
 "goblin": (1, "2d6", 12, 2, "1d6", 10, 4, (2, 4), []), "fire beetle": (1, "2d8", 14, 2, "1d6", 14, 0, (1, 3), []),
 "giant centipede": (1, "1d8", 12, 2, "1d4", 8, 0, (2, 4), ["poison"]), "grave moth": (1, "1d6", 13, 2, "1d3", 9, 0, (2, 5), ["sleeper"]),
 "hobgoblin": (2, "3d8", 14, 4, "1d8", 32, 10, (1, 3), []), "bandit": (2, "3d6", 13, 3, "1d8", 30, 14, (2, 3), []),
 "shadow": (2, "3d8", 14, 4, "1d6", 38, 0, (1, 2), ["undead"]), "carrion crawler": (2, "3d8", 12, 3, "1d4", 36, 0, (1, 2), ["paralyze"]),
 "minotaur": (3, "6d8", 14, 7, "2d6", 110, 15, (1, 1), []), "harpy": (3, "4d8", 13, 5, "1d8", 80, 12, (1, 3), ["sleeper"]),
 "cave bear": (3, "6d8", 13, 6, "1d10", 95, 0, (1, 2), []), "wight": (3, "5d8", 15, 6, "1d8", 105, 14, (1, 2), ["undead", "paralyze"]),
 "wyvern": (4, "8d8", 16, 8, "2d6", 260, 30, (1, 1), ["poison"]), "vampire spawn": (4, "7d8", 16, 8, "1d10", 240, 50, (1, 2), ["undead", "regen"]),
 "stone golem": (4, "9d8", 18, 8, "2d8", 300, 0, (1, 1), []), "death knight": (5, "11d8", 19, 10, "2d8", 800, 200, (1, 1), ["undead"]),
 "elder drake": (5, "13d8", 18, 10, "2d10", 1000, 400, (1, 1), ["breath"]),
}
BOSS_A = ["the Unwashed", "of the Ninth Stair", "the Patient", "Who Counts Bones", "the Hollow", "Gate-Eater", "the Last Tenant", "of the Wet Dark", "the Unpaid", "Lamp-Breaker"]
def tier_for(floor): return max(1, min(5, (floor + 1) // 2))

def make_group(name, n, floor, g=None, boss=False):
    t = MON[name]; df = DF(g)
    extra = max(0, floor - 2 * t[0])                      # deeper = tougher
    scale = df["hp"] * {1: .8, 2: 1.0, 3: 1.15, 4: 1.3}.get(psize(g), 1.3) * (1.25 if boss and df["hp"] > 1 else 1)
    mons = []
    for _ in range(n):
        m = {"hp": max(1, int(round((dice(t[1]) + extra * 2) * scale))), "st": []}
        if not boss and random.random() < df["elite"] * (.4 if floor <= 1 else 1): m["elite"] = True; m["hp"] = int(m["hp"] * 1.5) + 2
        m["max"] = m["hp"]; mons.append(m)
    return {"name": name, "mons": mons}

def _fresh(g, pool):
    """Variety: don't serve the monster that was just served. (g['recent'] = the last few kinds met.)"""
    rec = (g or {}).get("recent", [])
    p = [m for m in pool if m not in rec[-4:]] or [m for m in pool if m not in rec[-1:]] or pool
    m = random.choice(p)
    if g is not None: g["recent"] = (rec + [m])[-6:]
    return m

def encounter(floor, boss=False, g=None):
    t = tier_for(floor); df = DF(g); n = psize(g)
    pool = [k for k, v in MON.items() if v[0] == t] + [k for k, v in MON.items() if v[0] == t - 1]
    more = df["more"] if (floor >= 2 or df["more"] < 0) else max(0, df["more"] - 1)      # the first floor is where you find your feet
    size_adj = {1: -1, 2: 0, 3: 1, 4: 2}.get(n, 2) + more
    if boss:
        top = [k for k, v in MON.items() if v[0] == min(5, t + 1)]
        b = _fresh(g, top); gs = [make_group(b, 1, floor + 1, g, boss=True)]
        gs[0]["title"] = "%s %s" % (b.title(), random.choice(BOSS_A))
        if random.random() < .5 + .1 * max(0, size_adj): m = _fresh(g, pool); gs.append(make_group(m, max(1, random.randint(1, 2) + max(0, size_adj)), floor, g))
        return gs
    groups = []
    two = (floor >= 3 and random.random() < .3) or random.random() < df["two"] * (.5 if floor <= 1 else 1) or (n >= 3 and random.random() < .25)
    for i in range(2 if two else 1):
        m = _fresh(g, pool); lo, hi = MON[m][7]
        hi = max(lo, (hi + 1) // 2 + (1 if floor >= 3 else 0))       # the base size suits a party of two
        k = random.randint(lo, hi) + (size_adj if i == 0 else max(0, size_adj - 1))
        if lo == hi == 1 and MON[m][0] >= 3: k = 1 + (1 if size_adj >= 2 and random.random() < .5 else 0)   # big solitary things stay (mostly) solitary
        groups.append(make_group(m, max(1, k), floor, g))
    return groups

# ------------------------------------------------------------------- maze ---
N, E, S, W = 1, 2, 4, 8
DX = {N: (0, -1), E: (1, 0), S: (0, 1), W: (-1, 0)}
OPP = {N: S, S: N, E: W, W: E}
DIRS = {"n": N, "e": E, "s": S, "w": W}
DNAME = {N: "north", E: "east", S: "south", W: "west"}
RIGHT = {N: E, E: S, S: W, W: N}; LEFT = {v: k for k, v in RIGHT.items()}

def gen_floor(n, w=10, h=10):
    walls = [[15] * w for _ in range(h)]
    stack = [(0, 0)]; seen = {(0, 0)}
    while stack:
        x, y = stack[-1]
        nb = [(dr, x + DX[dr][0], y + DX[dr][1]) for dr in (N, E, S, W)
              if 0 <= x + DX[dr][0] < w and 0 <= y + DX[dr][1] < h and (x + DX[dr][0], y + DX[dr][1]) not in seen]
        if not nb: stack.pop(); continue
        dr, nx, ny = random.choice(nb)
        walls[y][x] &= ~dr; walls[ny][nx] &= ~OPP[dr]; seen.add((nx, ny)); stack.append((nx, ny))
    for _ in range(w * h // 8):                       # a few loops so it isn't a pure tree
        x, y = random.randrange(w), random.randrange(h); dr = random.choice((N, E, S, W))
        nx, ny = x + DX[dr][0], y + DX[dr][1]
        if 0 <= nx < w and 0 <= ny < h: walls[y][x] &= ~dr; walls[ny][nx] &= ~OPP[dr]
    dist = bfs(walls, (0, 0), w, h)
    far = max(dist, key=dist.get)
    cells = [c for c in dist if c not in ((0, 0), far) and dist[c] > 2]
    random.shuffle(cells)
    feat = {"%d,%d" % far: "down", "0,0": "up"}
    for kind, k in (("chest", 3), ("trap", 3), ("fountain", 1), ("shrine", 1)):
        for _ in range(k):
            if cells: c = cells.pop(); feat["%d,%d" % c] = kind
    prev = [c for c in dist if dist[c] == dist[far] - 1]
    if prev: feat["%d,%d" % prev[0]] = "guardian"
    return {"w": w, "h": h, "walls": walls, "seen": ["0,0"], "feat": feat}

def bfs(walls, start, w, h):
    dist = {start: 0}; q = [start]
    while q:
        x, y = q.pop(0)
        for dr in (N, E, S, W):
            if not walls[y][x] & dr:
                nx, ny = x + DX[dr][0], y + DX[dr][1]
                if (nx, ny) not in dist: dist[(nx, ny)] = dist[(x, y)] + 1; q.append((nx, ny))
    return dist

def floor_of(g): return g["floors"][str(g["floor"])]

def ascii_map(g):
    f = floor_of(g); w, h = f["w"], f["h"]; seen = set(f["seen"]); px, py = g["pos"]
    arrow = {N: "^", E: ">", S: "v", W: "<"}[g["facing"]]
    mark = {"down": "v", "up": "^", "chest": "$", "fountain": "~", "guardian": "!", "shrine": "+"}
    L = []
    for y in range(h):
        top = "+" if ("0,%d" % y) in seen or ("0,%d" % (y - 1)) in seen else " "; mid = ""
        for x in range(w):
            k = "%d,%d" % (x, y); vis = k in seen or ("%d,%d" % (x, y - 1)) in seen
            top += ("--" if f["walls"][y][x] & N else "  ") if vis else "  "
            near = vis or ("%d,%d" % (x + 1, y)) in seen or ("%d,%d" % (x + 1, y - 1)) in seen
            top += "+" if near else " "
            westvis = k in seen or ("%d,%d" % (x - 1, y)) in seen
            mid += ("|" if f["walls"][y][x] & W else " ") if westvis else " "
            if (x, y) == (px, py): mid += "@" + arrow
            elif k in seen:
                ft = f["feat"].get(k); mid += (mark.get(ft, ".") + " ") if ft and ft != "trap" else ". "
            else: mid += "  "
        lastk = "%d,%d" % (w - 1, y)
        mid += ("|" if f["walls"][y][w - 1] & E else " ") if lastk in seen else " "
        L.append(top); L.append(mid)
    bot = "".join(("+" if ("%d,%d" % (x, h - 1)) in seen or ("%d,%d" % (x - 1, h - 1)) in seen else " ") + (("--" if f["walls"][h - 1][x] & S else "  ") if ("%d,%d" % (x, h - 1)) in seen else "  ") for x in range(w)) + ("+" if ("%d,%d" % (w - 1, h - 1)) in seen else " ")
    L.append(bot)
    L = [l.rstrip() for l in L]
    L = [((("%2d " % (i // 2 + 1)) if i % 2 else "   ") + l) if l else "" for i, l in enumerate(L)]      # row numbers down the side
    while L and not L[-1]: L.pop()
    while L and not L[0]: L.pop(0)
    hdr = "    " + "".join("%s  " % chr(65 + x) for x in range(w)).rstrip()                              # column letters on top
    return hdr + "\n" + "\n".join(L) + "\n@ you  ^v stairs  $ chest  ~ fountain  + shrine  ! guardian  (blank = unexplored)\n" + where_text(g)

# ---- WALK BY NAMING A SQUARE. Where to go is a shared decision, so it is one decision: the map has letters across and
# numbers down, and whoever's call it is names a square - any explored one, or an unexplored one beside an opening.
# The party walks there and stops if something happens on the way.
def cell_name(x, y): return "%s%d" % (chr(65 + x), y + 1)
def cell_parse(s, f):
    m = re.fullmatch(r"([a-zA-Z])\s*(\d{1,2})", (s or "").strip())
    if not m: return None
    x = ord(m.group(1).upper()) - 65; y = int(m.group(2)) - 1
    return (x, y) if 0 <= x < f["w"] and 0 <= y < f["h"] else None
def path_to(g, target):
    """Shortest walk over explored squares (the last step may enter an unexplored one). -> [directions] or None"""
    f = floor_of(g); seen = set(f["seen"]); start = tuple(g["pos"]); prev = {start: None}; q = [start]
    while q:
        x, y = q.pop(0)
        if (x, y) == target: break
        for dr in (N, E, S, W):
            if f["walls"][y][x] & dr: continue
            nx, ny = x + DX[dr][0], y + DX[dr][1]
            if not (0 <= nx < f["w"] and 0 <= ny < f["h"]) or (nx, ny) in prev: continue
            if "%d,%d" % (nx, ny) not in seen and (nx, ny) != target: continue
            prev[(nx, ny)] = ((x, y), dr); q.append((nx, ny))
    if target not in prev: return None
    steps, cur = [], target
    while prev[cur]: p, dr = prev[cur]; steps.append(dr); cur = p
    return steps[::-1]
def frontier(g):
    """Unexplored squares you can see an opening into: [(cell, steps, 'east of C4')], nearest first."""
    f = floor_of(g); seen = set(f["seen"]); out = {}
    for k in seen:
        x, y = map(int, k.split(","))
        for dr in (N, E, S, W):
            if f["walls"][y][x] & dr: continue
            nx, ny = x + DX[dr][0], y + DX[dr][1]
            if 0 <= nx < f["w"] and 0 <= ny < f["h"] and "%d,%d" % (nx, ny) not in seen and (nx, ny) not in out:
                p = path_to(g, (nx, ny))
                if p is not None: out[(nx, ny)] = (len(p), "%s of %s" % (DNAME[dr], cell_name(x, y)))
    return sorted([(cell_name(*c), v[0], v[1]) for c, v in out.items()], key=lambda z: z[1])
def where_text(g):
    f = floor_of(g); seen = set(f["seen"]); L = ["You are at %s on floor %d." % (cell_name(*g["pos"]), g["floor"])]
    names = {"down": "stairs DOWN", "up": "stairs up", "chest": "a chest", "fountain": "a fountain", "shrine": "a shrine", "guardian": "the GUARDIAN"}
    known_ = ["%s at %s" % (names[v], cell_name(*map(int, k.split(",")))) for k, v in f["feat"].items() if k in seen and v in names and k != "%d,%d" % tuple(g["pos"])]
    if known_: L.append("Known: " + ", ".join(known_) + ".")
    fr = frontier(g)
    if fr: L.append("Unexplored, within reach: " + ", ".join("%s (%d step%s, %s)" % (c, n, "" if n == 1 else "s", w) for c, n, w in fr[:7]) + (" ... and %d more" % (len(fr) - 7) if len(fr) > 7 else "") + ".")
    else: L.append("Nothing unexplored is left within reach on this floor.")
    L.append("Call: %s.  goto <square>  (e.g. goto %s)" % (g.get("leader", "?"), fr[0][0] if fr else cell_name(*g["pos"])))
    return "\n".join(L)

def cmd_goto(g, a, img):
    in_dungeon(g)
    if g["mode"] == "doors": die("Doors mode: delve.py doors / pick 1|2|3")
    f = floor_of(g)
    if not a: print(ascii_map(g)); return
    tgt = cell_parse("".join(a), f) or die("goto <square> - a letter and a number from the map, e.g. goto C4.\n" + where_text(g))
    if tuple(g["pos"]) == tgt: die("You're standing on %s." % cell_name(*tgt))
    path = path_to(g, tgt)
    if path is None:
        die("No known way to %s yet - you can walk to any explored square, or to an unexplored one right beside an opening.\n%s" % (cell_name(*tgt), where_text(g)))
    needs_word(g, g.get("leader", "?"), "call_opened", "call (where to go)")
    moved = 0; g["peddler"] = False
    for dr in path:
        x, y = g["pos"]; g["facing"] = dr
        g["came_from"] = [x, y]; g["pos"] = [x + DX[dr][0], y + DX[dr][1]]; moved += 1
        k = "%d,%d" % tuple(g["pos"]); trod = k in f["seen"]
        if not trod: f["seen"].append(k)
        ft = f["feat"].get(k)
        if ft in ("chest", "trap", "fountain", "guardian", "shrine"):
            if ft != "guardian": f["feat"][k] = "spent" if ft != "fountain" else "dryfountain"
            if event(g, ft): break
        if g["floor"] == 0: break
        if random_fight(g, trod): break
    if g["floor"] == 0: save(g); return                     # the party fell (a trap) and woke in town
    short_ = tuple(g["pos"]) != tgt
    print("Walked %d step%s to %s%s." % (moved, "" if moved == 1 else "s", cell_name(*g["pos"]), (" - stopped on the way to %s" % cell_name(*tgt)) if short_ else ""))
    busy = g.get("fight") or g.get("chest") or g.get("loot") or g.get("shrine")
    ft = f["feat"].get("%d,%d" % tuple(g["pos"]))
    if ft in ("down", "up") and not busy: print("Stairs %s are here: delve.py stairs" % ft)
    if not busy: next_call_print(g); print(where_text(g))
    if img and not g.get("fight"): print("image: " + draw_map(g))
    if img and g.get("fight"): print("image: " + draw_fight(g))
    save(g)

def ahead_text(g):
    f = floor_of(g); x, y = g["pos"]; fc = g["facing"]; parts = []
    for side, dr in (("left", LEFT[fc]), ("right", RIGHT[fc])):
        if not f["walls"][y][x] & dr: parts.append("opening %s" % side)
    steps = 0; cx, cy = x, y
    while not f["walls"][cy][cx] & fc and steps < 6:
        cx, cy = cx + DX[fc][0], cy + DX[fc][1]; steps += 1
    parts.insert(0, ("wall ahead" if steps == 0 else "corridor %d ahead" % steps))
    return "Facing %s. %s." % (DNAME[fc], ", ".join(parts))

# ------------------------------------------------------------------- png ---
class Canvas:
    def __init__(s, w, h, bg=(0, 0, 0)): s.w, s.h = w, h; s.px = [[bg] * w for _ in range(h)]
    def set(s, x, y, c):
        if 0 <= x < s.w and 0 <= y < s.h: s.px[y][x] = c
    def rect(s, x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(s.h, y1)):
            for x in range(max(0, x0), min(s.w, x1)): s.px[y][x] = c
    def line(s, x0, y0, x1, y1, c):
        dx, dy = abs(x1 - x0), -abs(y1 - y0); sx = 1 if x0 < x1 else -1; sy = 1 if y0 < y1 else -1; err = dx + dy
        while True:
            s.set(x0, y0, c)
            if x0 == x1 and y0 == y1: break
            e2 = 2 * err
            if e2 >= dy: err += dy; x0 += sx
            if e2 <= dx: err += dx; y0 += sy
    def trap(s, x0, x1, t0, b0, t1, b1, c):        # vertical-edged quad: x0..x1, top/bottom interpolate
        if x1 < x0: x0, x1, t0, b0, t1, b1 = x1, x0, t1, b1, t0, b0
        for x in range(x0, x1 + 1):
            k = (x - x0) / max(1, x1 - x0); t = int(t0 + (t1 - t0) * k); b = int(b0 + (b1 - b0) * k)
            for y in range(t, b + 1): s.set(x, y, c)
    def save(s, path, scale=4):
        raw = b""
        for row in s.px:
            line = b"".join(bytes(c) * scale for c in row)
            raw += (b"\x00" + line) * scale
        def chunk(t, data): return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xffffffff)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", s.w * scale, s.h * scale, 8, 2, 0, 0, 0)) \
              + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
        os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(png); return path

FRAMES = [(0, 0, 96, 64), (14, 9, 82, 55), (26, 17, 70, 47), (34, 22, 62, 42), (39, 26, 57, 38)]
def draw_view(g, cv=None):
    f = floor_of(g); x, y = g["pos"]; fc = g["facing"]
    cv = cv or Canvas(96, 64, (8, 8, 14))
    cv.rect(0, 32, 96, 64, (20, 18, 16)); cv.rect(0, 0, 96, 32, (10, 10, 18))
    shade = [(150, 150, 165), (115, 115, 130), (85, 85, 100), (60, 60, 72)]
    edge = (210, 210, 225)
    for dep in range(4):
        L0, T0, R0, B0 = FRAMES[dep]; L1, T1, R1, B1 = FRAMES[dep + 1]; col = shade[dep]
        w_ = f["walls"][y][x]
        if w_ & LEFT[fc]: cv.trap(L0, L1, T0, B0, T1, B1, col); cv.line(L0, T0, L1, T1, edge); cv.line(L0, B0, L1, B1, edge); cv.line(L1, T1, L1, B1, edge)
        else: cv.rect(L0, T1, L1, B1 + 1, shade[min(3, dep + 1)]); cv.line(L0, T1, L1, T1, edge); cv.line(L0, B1, L1, B1, edge)
        if w_ & RIGHT[fc]: cv.trap(R1, R0, T1, B1, T0, B0, col); cv.line(R0, T0, R1, T1, edge); cv.line(R0, B0, R1, B1, edge); cv.line(R1, T1, R1, B1, edge)
        else: cv.rect(R1, T1, R0, B1 + 1, shade[min(3, dep + 1)]); cv.line(R1, T1, R0, T1, edge); cv.line(R1, B1, R0, B1, edge)
        if w_ & fc:
            cv.rect(L1, T1, R1 + 1, B1 + 1, col)
            for (a, b, c_, e_) in ((L1, T1, R1, T1), (L1, B1, R1, B1), (L1, T1, L1, B1), (R1, T1, R1, B1)): cv.line(a, b, c_, e_, edge)
            ft = f["feat"].get("%d,%d" % (x, y))
            break
        x, y = x + DX[fc][0], y + DX[fc][1]
        ft = f["feat"].get("%d,%d" % (x, y))
        if ft in ("down", "up") and "%d,%d" % (x, y) in f["seen"]:
            Lc, Tc, Rc, Bc = FRAMES[dep + 1]; cv.rect((Lc + Rc) // 2 - 4, Bc - 3, (Lc + Rc) // 2 + 4, Bc, (200, 180, 60))
    return cv

# 3x5 pixel font for the stat panel
FONT = {
 "A": "010101111101101", "B": "110101110101110", "C": "011100100100011", "D": "110101101101110", "E": "111100110100111",
 "F": "111100110100100", "G": "011100101101011", "H": "101101111101101", "I": "111010010010111", "J": "001001001101010",
 "K": "101101110101101", "L": "100100100100111", "M": "101111111101101", "N": "110101101101101", "O": "010101101101010",
 "P": "110101110100100", "Q": "010101101110011", "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
 "U": "101101101101111", "V": "101101101101010", "W": "101101111111101", "X": "101101010101101", "Y": "101101010010010",
 "Z": "111001010100111", "0": "111101101101111", "1": "010110010010111", "2": "110001010100111", "3": "110001010001110",
 "4": "101101111001001", "5": "111100110001110", "6": "011100111101111", "7": "111001010010010", "8": "111101111101111",
 "9": "111101111001110", ">": "100010001010100", "'": "010010000000000", ",": "000000000010100", "/": "001001010100100", ":": "000010000010000", "-": "000000111000000", "+": "000010111010000",
 " ": "000000000000000", ".": "000000000000010", "!": "010010010000010", "?": "110001010000010", "(": "010100100100010", ")": "010001001001010"}
def text(cv, x, y, s, col):
    for ch in s.upper():
        gl = FONT.get(ch, FONT["?"])
        for i, b in enumerate(gl):
            if b == "1": cv.set(x + i % 3, y + i // 3, col)
        x += 4
    return x

def bar(cv, x, y, w, frac, col):
    cv.rect(x, y, x + w, y + 3, (40, 40, 48)); cv.rect(x, y, x + max(0, int(w * max(0, min(1, frac)))), y + 3, col)

def hpcol(frac): return (80, 200, 90) if frac > .5 else ((230, 190, 60) if frac > .25 else (220, 60, 60))

def short(line):
    """A round's log line squeezed for the picture (23 chars of the 3x5 font)."""
    t = " " + line.strip().upper() + " "
    for a_, b_ in ((" SWINGS AT A ", " MISSES "), (" - MISS.", ""), (" - IT DIES.", " DIES"), (" HITS A ", " HITS "), (" FOR ", " "),
                   (" A ", " "), (".", ""), ("!", ""), (":", "")):
        t = t.replace(a_, b_)
    return re.sub(r"\s+", " ", t).strip()[:23]

def with_hud(g, cv, foes=True):
    """Phone-portrait card (9:16): header, the scene, foes, a card per hero (level, HP/MP/XP bars, status, command
    menu), and what just happened. Everything a decision needs, at a glance - also after a week away."""
    W = 96; fight = g.get("fight") or {}
    groups = [gr for gr in fight.get("groups", []) if any(m["hp"] > 0 for m in gr["mons"])] if foes else []
    menus = bool(groups)
    rows = []                                   # (height, draw-fn)
    big_h = 9 + 64 + 2
    hero_h = lambda c: 7 + 6 + (6 if c["maxmp"] else 0) + 6 + (6 if [x for x in c["st"] if x in ("poisoned", "asleep", "paralyzed", "down")] else 0) + (6 * len(menu_lines(g, c)) if menus and can_act(c) else 0) + 3
    big_h += (4 + 7 * len(groups)) if groups else 0
    big_h += sum(hero_h(c) for c in g["party"]) + 2
    last = [short(x) for x in (fight.get("last") or g.get("last_event") or [])][-3:]
    big_h += (4 + 6 * len(last)) if last else 0
    H = max(big_h + 2, int(W * 16 / 9))
    big = Canvas(W, H, (14, 14, 20))
    # header
    big.rect(0, 0, W, 8, (28, 26, 38))
    hdr = ("FL %d" % g["floor"]) if g["floor"] else "TOWN"
    if g.get("mode") == "doors" and g["floor"]: hdr += " DOOR %d/%d" % (min(g.get("step", 0) + (0 if fight else 1), DOOR_STEPS) or 1, DOOR_STEPS)
    if fight: hdr += "  ROUND %d" % fight.get("round", 1)
    text(big, 2, 2, hdr, (210, 210, 225))
    # scene
    for yy in range(64): big.px[9 + yy] = list(cv.px[yy])
    text(big, 2, 11, "POT %d" % g.get("potions", 0), (240, 130, 170))
    gs = "GOLD %d" % min(g["gold"], 99999); text(big, W - 2 - 4 * len(gs), 11, gs, (240, 210, 90))
    y = 9 + 64 + 2
    if groups:
        text(big, 2, y, "FOES", (150, 150, 160)); y += 6
        for gr in groups:
            live = [m for m in gr["mons"] if m["hp"] > 0]
            fr = sum(m["hp"] for m in live) / max(1, sum(m["max"] for m in gr["mons"]))
            text(big, 2, y, ("%s X%d" % (gr["name"], len(live)))[:14], (240, 170, 120))
            bar(big, 60, y + 1, 34, fr, hpcol(fr)); y += 7
        y += 1
    for c in g["party"]:
        big.rect(1, y - 1, W - 1, y, (40, 40, 52))
        down = c["hp"] <= 0
        text(big, 2, y + 1, c["name"][:7], (235, 235, 245) if not down else (160, 90, 90))
        text(big, 34, y + 1, "L%d %s" % (c["lvl"], c["cls"][:7]), (160, 160, 175))
        y += 7
        fr = c["hp"] / max(1, c["maxhp"])
        text(big, 2, y, "HP", (160, 160, 175)); bar(big, 12, y + 1, 44, fr, hpcol(fr)); text(big, 59, y, "%d/%d" % (max(0, c["hp"]), c["maxhp"]), (230, 230, 240)); y += 6
        if c["maxmp"]:
            text(big, 2, y, "MP", (160, 160, 175)); bar(big, 12, y + 1, 44, c["mp"] / max(1, c["maxmp"]), (100, 140, 255)); text(big, 59, y, "%d/%d" % (c["mp"], c["maxmp"]), (140, 170, 255)); y += 6
        nxt = XP_AT[c["lvl"]] if c["lvl"] < len(XP_AT) else c["xp"]; prev = XP_AT[c["lvl"] - 1]
        text(big, 2, y, "XP", (160, 160, 175)); bar(big, 12, y + 1, 44, (c["xp"] - prev) / max(1, nxt - prev), (200, 160, 255)); text(big, 59, y, "%d/%d" % (c["xp"], nxt), (200, 175, 255)); y += 6
        st = [s_ for s_ in c["st"] if s_ in ("poisoned", "asleep", "paralyzed", "down")]
        if st:
            col = {"poisoned": (90, 200, 90), "asleep": (120, 160, 255), "paralyzed": (230, 190, 60), "down": (220, 60, 60)}[st[0]]
            text(big, 2, y, " ".join(st).upper(), col); y += 6
        if menus and can_act(c):
            for k, row in enumerate(menu_lines(g, c)): text(big, 4, y, ("> " if k == 0 else "  ") + row, (170, 200, 255)); y += 6
        y += 3
    if last:
        big.rect(1, y, W - 1, y + 1, (40, 40, 52)); y += 3
        for l in last: text(big, 2, y, l, (190, 190, 200)); y += 6
    if not fight and g.get("mode") == "doors" and g.get("doorset") and y + 26 < H:
        big.rect(1, y, W - 1, y + 1, (40, 40, 52)); y += 3
        text(big, 2, y, "CALL: %s" % g.get("leader", "?").upper(), (240, 210, 90)); y += 7
        for i, dd in enumerate(g["doorset"]):
            words, rows, cur = dd["hint"].upper().split(), [], ""
            for w in words:
                if len(cur) + len(w) + 1 > 21: rows.append(cur); cur = ""
                cur += (" " if cur else "") + w
            rows.append(cur)
            for k, r in enumerate(rows[:2]): text(big, 2, y, ("%d " % (i + 1) if k == 0 else "  ") + r, (200, 200, 215)); y += 6
            y += 1
    return big

def menu_lines(g, c):
    """Final-Fantasy-style command list for a hero: up to two rows of 22 chars."""
    o = ["ATK", "DEF"] + (["POT%d" % g["potions"]] if g["potions"] else []) + [sp.upper() for sp in known(c) if c["mp"] >= SPELLS[sp][1]]
    rows, cur = [], ""
    for w in o:
        if len(cur) + len(w) + 1 > 21:
            rows.append(cur); cur = ""
            if len(rows) == 2: break
        cur += (" " if cur else "") + w
    if cur and len(rows) < 2: rows.append(cur)
    return rows

def sprite(name, color):
    """10x10 creature: a body silhouette chosen by the name's hash, mirrored, with eyes."""
    h = hashlib.md5(name.encode()).digest()
    shapes = [  # half-width rows (5 cols) - 1 = body
        ["00011", "00111", "01111", "01111", "11111", "11111", "01111", "01111", "01010", "01010"],   # biped
        ["00000", "00011", "00111", "01111", "11111", "11111", "11111", "01111", "10101", "00000"],   # beast/blob
        ["10001", "11011", "01111", "00111", "01111", "11111", "10111", "00111", "00101", "01001"],   # winged/bat
        ["00111", "01111", "01111", "00111", "00011", "00111", "01111", "11111", "11011", "10001"],   # robed/caster
    ]
    tags = MON.get(name, (0,) * 9)[8] if name in MON else []
    sh = shapes[3] if "caster" in tags or "sleeper" in tags else shapes[h[0] % 3]
    bits = []
    for y in range(10):
        half = [int(ch) for ch in sh[y]]
        for x in range(5):
            if half[x] and y > 3 and (h[(y * 5 + x) % 16] >> (x % 8)) & 7 == 0: half[x] = 2      # texture
        bits.append(half + half[::-1])
    for y, x in ((2, 3), (2, 6)): bits[y][x] = 3                                                    # eyes
    return bits, color
TCOL = {"undead": (190, 200, 170), "poison": (90, 200, 90), "caster": (170, 110, 220), "breath": (230, 90, 40)}
def room_bg():
    cv = Canvas(96, 64, (10, 10, 16)); cv.rect(0, 40, 96, 64, (24, 21, 18)); cv.rect(0, 0, 96, 40, (34, 32, 40))
    for x in range(0, 96, 12): cv.line(x, 0, x, 40, (44, 42, 52))
    for y in range(0, 40, 8): cv.line(0, y, 96, y, (44, 42, 52))
    cv.line(0, 40, 96, 40, (90, 90, 100)); return cv

def draw_fight(g):
    cv = draw_view(g) if g.get("mode") == "maze" and str(g.get("floor")) in g.get("floors", {}) else room_bg()
    groups = [gr for gr in g["fight"]["groups"] if any(m["hp"] > 0 for m in gr["mons"])]
    plan = []
    for gr in groups:
        tags = MON[gr["name"]][8]; col = next((TCOL[t] for t in tags if t in TCOL), (200, 120, 90))
        plan.append((gr, min(3, sum(1 for m in gr["mons"] if m["hp"] > 0)), col))
    total = sum(c for _, c, _ in plan)
    sc = 3 if total == 1 else 2 if total <= 4 else 1
    w = 10 * sc; gap = 2 * sc; grp_gap = 6
    width = total * w + (total - len(plan)) * gap + (len(plan) - 1) * grp_gap
    x = 48 - width // 2
    for gr, cnt, col in plan:
        bits, col = sprite(gr["name"], col); dark = tuple(max(0, v - 60) for v in col)
        for j in range(cnt):
            oy = 46 - 10 * sc + (j % 2) * 2
            for yy in range(10):
                for xx in range(10):
                    v = bits[yy][xx]
                    if v: cv.rect(x + xx * sc, oy + yy * sc, x + xx * sc + sc, oy + yy * sc + sc, {1: col, 2: dark, 3: (255, 240, 120)}[v])
            x += w + gap
        x += grp_gap - gap
    return with_hud(g, cv).save(os.path.join(OUT, "fight.png"))

def draw_map(g):
    f = floor_of(g); w, h = f["w"], f["h"]; cs = 8; seen = set(f["seen"])
    cv = Canvas(w * cs + 1, h * cs + 1, (0, 0, 0)); wall = (220, 220, 230)
    for k in seen:
        x, y = map(int, k.split(",")); cv.rect(x * cs + 1, y * cs + 1, x * cs + cs, y * cs + cs, (45, 45, 55))
        ft = f["feat"].get(k)
        col = {"down": (230, 200, 60), "up": (120, 200, 230), "chest": (230, 170, 40), "fountain": (70, 140, 240), "guardian": (220, 60, 60), "shrine": (230, 230, 240)}.get(ft)
        if col: cv.rect(x * cs + 3, y * cs + 3, x * cs + 6, y * cs + 6, col)
        wl = f["walls"][y][x]
        if wl & N: cv.line(x * cs, y * cs, x * cs + cs, y * cs, wall)
        if wl & S: cv.line(x * cs, y * cs + cs, x * cs + cs, y * cs + cs, wall)
        if wl & W: cv.line(x * cs, y * cs, x * cs, y * cs + cs, wall)
        if wl & E: cv.line(x * cs + cs, y * cs, x * cs + cs, y * cs + cs, wall)
    px, py = g["pos"]; cx, cy = px * cs + 4, py * cs + 4
    cv.rect(cx - 2, cy - 2, cx + 2, cy + 2, (240, 60, 60))
    dx, dy = DX[g["facing"]]; cv.line(cx, cy, cx + dx * 3, cy + dy * 3, (255, 255, 255))
    big = Canvas(cv.w + 9, cv.h + 7, (0, 0, 0))             # letters across the top, numbers down the side
    for yy in range(cv.h): big.px[yy + 7][9:9 + cv.w] = cv.px[yy]
    for x in range(w): text(big, 9 + x * cs + 3, 1, chr(65 + x), (240, 210, 90))
    for y in range(h): text(big, 0 if y >= 9 else 4, 7 + y * cs + 2, str(y + 1), (240, 210, 90))
    return big.save(os.path.join(OUT, "map.png"), scale=3)

# ---------------------------------------------------------------- fights ---
def start_fight(g, groups, boss=False, why=""):
    g["fight"] = {"groups": groups, "orders": {}, "round": 1, "ward": False, "boss": boss}; g["opened"] = now_iso()
    for c in g["party"]: c["st"] = [s for s in c["st"] if s in ("poisoned",)]
    desc = ", ".join((gr["title"] if gr.get("title") else "%d %s" % (len(gr["mons"]), gr["name"] + ("s" if len(gr["mons"]) > 1 else "")))
                     + ((" (%d ELITE)" % sum(1 for m in gr["mons"] if m.get("elite"))) if any(m.get("elite") for m in gr["mons"]) else "") for gr in groups)
    print("%s%s!  %s" % ("GUARDIAN" if boss else "ENCOUNTER", (" - " + why) if why else "", desc)); log(g, "fight: " + desc)
    had = [c["name"] for c in g["party"] if c.pop("stance", None)]      # a new fight is a new decision
    if had: print("New fight - the standing order from the last one is over (%s). Each hero needs an order, or a new stance, for this one." % ", ".join(had))
    picture(g, "fight")
    print(orders_needed(g))

def send_photo(path, caption):
    """Optional: post the picture to a Telegram chat (delve_local.json "send": {"telegram_chat": ...}). The bot token
    comes from the DELVE_TG_TOKEN environment variable, or - on an OpenClaw install - from its own config. Never from this repo."""
    import urllib.request, uuid
    tok = os.environ.get("DELVE_TG_TOKEN")
    if not tok:
        try: tok = (json.load(open(os.path.expanduser("~/.openclaw/openclaw.json"))).get("channels", {}).get("telegram", {}) or {}).get("botToken")
        except Exception: tok = None
    if not tok or not TG_CHAT: raise RuntimeError("no bot token / chat")
    b = uuid.uuid4().hex; body = b""
    for k, v in (("chat_id", TG_CHAT), ("caption", caption[:1000])):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"delve.png\"\r\nContent-Type: image/png\r\n\r\n" % b).encode()
    body += open(path, "rb").read() + ("\r\n--%s--\r\n" % b).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendPhoto" % tok, body, {"Content-Type": "multipart/form-data; boundary=%s" % b})
    urllib.request.urlopen(req, timeout=20).read()

def picture(g, kind):
    """Pictures at every fight round and new floor. The script can send them itself (captioned with the
    board) so a round costs her one command and one line of prose. DELVE_NOSEND=1 (tests) just writes the file."""
    if not g.get("pics", True): return
    if kind != "fight" and g.get("mode") == "doors" and g["floor"] and not g.get("doorset") and not g.get("stairs_open"): g["doorset"] = new_doors(g)
    try:
        path = draw_fight(g) if kind == "fight" else (draw_doors(g) if g["mode"] == "doors" else draw_map(g))
    except Exception as e: print("(picture failed: %s)" % e); return
    if os.environ.get("DELVE_NOSEND") or not TG_CHAT or not path.startswith(os.path.join(HERE, "out")):
        print("PICTURE: %s" % path); return
    # Sent by a detached helper so a slow upload can't hold the command. The helper keeps its own copy; failures -> out/send.log.
    try:
        import subprocess, shutil as _sh
        snap = os.path.join(OUT, "sending-%d.png" % (int(time.time() * 1000) % 100000)); _sh.copyfile(path, snap)
        cap = snap + ".txt"; open(cap, "w", encoding="utf-8").write(board(g))
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_send", snap, cap], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print("PICTURE: already sent (with the board as its caption) - don't resend it.")
    except Exception as e: print("PICTURE: %s  (auto-send failed: %s - send it yourself)" % (path, str(e)[:80]))

def board(g):
    """The whole state in a few lines - everything a decision needs, nothing from earlier turns."""
    where = "town" if g["floor"] == 0 else ("Floor %d" % g["floor"] + (", door %d/%d" % (min(g.get("step", 0) + 1, DOOR_STEPS), DOOR_STEPS) if g["mode"] == "doors" else ""))
    L = ["%s | %s | gold %d | potions %d" % (where, g.get("diff", "normal").upper(), g["gold"], g["potions"])]
    for c in g["party"]:
        L.append("%s (%s %s L%d): HP %d/%d%s%s" % (c["name"], c["owner"], c["cls"], c["lvl"], max(0, c["hp"]), c["maxhp"],
                 (" MP %d/%d" % (c["mp"], c["maxmp"])) if c["maxmp"] else "", (" [" + ",".join(c["st"]) + "]") if c["st"] else ""))
    if g.get("fight"):
        L.append("Foes: " + ", ".join("%s x%d" % (gr["name"], sum(1 for m in gr["mons"] if m["hp"] > 0)) for gr in living_groups(g)))
    if g.get("loot"): L.append("LOOT waiting: %s  (loot need <Name> | loot pass <Name>)" % g["loot"]["item"])
    if g.get("shrine"): L.append("A shrine: shrine pray <Name> | shrine leave")
    for c in g["party"]: L += pick_line(c)
    return "\n".join(L)

def living_groups(g): return [gr for gr in g["fight"]["groups"] if any(m["hp"] > 0 for m in gr["mons"])]
def orders_needed(g):
    miss = [c for c in g["party"] if can_act(c) and c["name"] not in g["fight"]["orders"] and not stance_active(g, c)]
    st = ["%s will %s" % (c["name"], stance_active(g, c)) for c in g["party"] if stance_active(g, c) and can_act(c)]
    paused = [c["name"] for c in g["party"] if c.get("stance") == "attack-till-hurt" and not stance_active(g, c) and can_act(c)]
    gl = living_groups(g)
    foes = "  ".join("[%d] %s x%d%s" % (i + 1, gr.get("title") or gr["name"], sum(1 for m in gr["mons"] if m["hp"] > 0),
                     (" (%d ELITE)" % sum(1 for m in gr["mons"] if m["hp"] > 0 and m.get("elite"))) if any(m["hp"] > 0 and m.get("elite") for m in gr["mons"]) else "") for i, gr in enumerate(gl))
    lines = ["Foes: %s" % foes]
    if paused: lines.append("!! %s below half HP - auto-attack paused for %s: real orders this round." % (", ".join(c["name"] for c in hurt(g)), ", ".join(paused)))
    for c in miss: lines.append("%s (%s) can: %s" % (c["name"], c["owner"], options(g, c)))
    if st: lines.append("[standing: " + "; ".join(st) + "]")
    lines.append("(do <Name> attack [group] | cast <spell> [target] | defend | potion [ally]   - or the party: run)")
    return "\n".join(lines)

def options(g, c):
    o = ["attack (%s%s)" % ("%dd%d" % WEAPONS[c["wpn"]], "+%d" % dmg_bonus(c) if dmg_bonus(c) else "")]
    for sp in known(c):
        cost = SPELLS[sp][1]
        if c["mp"] >= cost: o.append("%s (%dMP: %s)" % (sp, cost, SPELL_TXT[sp]))
    o.append("defend (+4 AC this round)")
    if g["potions"]: o.append("potion (%d left: +2d8+2 HP, cures poison)" % g["potions"])
    return " | ".join(o)

def pick_group(g, arg):
    gl = living_groups(g)
    if arg and arg.isdigit() and 1 <= int(arg) <= len(gl): return gl[int(arg) - 1]
    if arg:
        for gr in gl:
            if gr["name"].startswith(arg.lower()): return gr
    return gl[0]

def cmd_do(g, a):
    if not g.get("fight"): die("No fight on.")
    if len(a) < 2: die("do <Name> attack [group] | cast <spell> [group|ally] | defend | potion [ally]")
    c = pc(g, a[0]); act = a[1].lower(); rest = a[2:]
    if not can_act(c): die("%s can't act right now (%s)." % (c["name"], ", ".join(c["st"]) or "down"))
    needs_word(g, c["owner"], "opened", "hero's order this round")
    if act == "cast":
        if not rest: die("Cast what? %s knows: %s" % (c["name"], ", ".join(known(c)) or "nothing yet"))
        sp = rest[0].lower()
        if sp not in known(c): die("%s doesn't know %s. Knows: %s" % (c["name"], sp, ", ".join(known(c)) or "nothing yet"))
        if c["mp"] < SPELLS[sp][1]: die("%s has %d MP; %s costs %d." % (c["name"], c["mp"], sp, SPELLS[sp][1]))
    if act == "potion" and g["potions"] <= 0: die("No potions left.")
    if act not in ("attack", "cast", "defend", "potion"): die("attack | cast <spell> | defend | potion")
    g["fight"]["orders"][c["name"]] = [act] + rest; save(g)
    miss = [x for x in g["party"] if can_act(x) and x["name"] not in g["fight"]["orders"] and not stance_active(g, x)]
    if miss: print("%s: %s. Waiting on %s." % (c["name"], " ".join([act] + rest), ", ".join("%s (%s)" % (x["name"], x["owner"]) for x in miss))); return
    try_resolve(g)

def mon_init(gr): return MON[gr["name"]][3] + random.randint(1, 10)

def resolve_round(g, monsters_only=False):
    f = g["fight"]; out = []
    for c in g["party"]:
        c["st"] = [s for s in c["st"] if s != "defending"]
        if not monsters_only and f["orders"].get(c["name"], [""])[0] == "defend": c["st"].append("defending")
    actors = []
    if not monsters_only:
        for c in g["party"]:
            if can_act(c) and c["name"] in f["orders"]:
                actors.append((bonus(c["s"]["agi"]) + random.randint(1, 10) + (3 if c["cls"] == "thief" else 0) + (4 if c.get("waff") == "swift" else 0), "pc", c))
    for gr in f["groups"]:
        for m in gr["mons"]:
            if m["hp"] > 0: actors.append((mon_init(gr), "mon", (gr, m)))
    actors.sort(key=lambda t: -t[0])
    for _, kind, who in actors:
        if not living_groups(g): break
        if kind == "pc": pc_act(g, who, f["orders"][who["name"]], out)
        else: mon_act(g, who[0], who[1], out)
        if not [c for c in g["party"] if c["hp"] > 0]: break
    for c in g["party"]:                                  # end of round
        if c["hp"] > 0 and "poisoned" in c["st"]:
            p = random.randint(1, 3); c["hp"] -= p; out.append("%s takes %d poison." % (c["name"], p)); down_check(c, out)
        if "asleep" in c["st"] and random.random() < .5: c["st"].remove("asleep"); out.append("%s wakes." % c["name"])
        if "paralyzed" in c["st"] and random.random() < .3: c["st"].remove("paralyzed"); out.append("%s can move again." % c["name"])
        if c["hp"] > 0 and charm(c, "ring of mending") and c["hp"] < c["maxhp"]: c["hp"] += 1
        if c["hp"] > 0 and has(c, "manaflow") and c["mp"] < c["maxmp"]: c["mp"] += 1
    for gr in f["groups"]:
        for m in gr["mons"]:                              # venom and fire keep working
            if m["hp"] <= 0: continue
            dot = (2 if "venom" in m["st"] else 0) + (2 if m.get("burn", 0) > 0 else 0)
            if m.get("burn", 0) > 0: m["burn"] -= 1
            if dot:
                m["hp"] -= dot
                if m["hp"] <= 0: m["hp"] = 0; out.append("A %s succumbs (%s)." % (gr["name"], "venom" if "venom" in m["st"] else "fire"))
    for gr in f["groups"]:
        if "regen" in MON[gr["name"]][8]:
            for m in gr["mons"]:
                if 0 < m["hp"] < m["max"]: m["hp"] = min(m["max"], m["hp"] + 3)
    print("\n".join(out))
    f["last"] = [l for l in out if l.strip() and not l.startswith("--")][-3:]
    f["orders"] = {}; f["round"] += 1; g["opened"] = now_iso()
    if not living_groups(g): return win(g)
    if not [c for c in g["party"] if c["hp"] > 0]: return wipe(g)
    save(g); print("--"); print(status_line(g)); picture(g, "fight"); print(orders_needed(g))

def status_line(g): return "Potions %d | " % g.get("potions", 0) + " | ".join("%s %d/%d%s%s" % (c["name"], max(0, c["hp"]), c["maxhp"], (" MP%d" % c["mp"]) if c["maxmp"] else "",
                                                        (" (" + ",".join(c["st"]) + ")") if c["st"] else "") for c in g["party"])

def down_check(c, out):
    if c["hp"] <= 0 and "down" not in c["st"]:
        c["hp"] = 0; c["st"] = ["down"]; out.append("%s is DOWN." % c["name"])

def hit_mon(g, gr, m, dmg, out, who):
    m["hp"] -= dmg
    if "asleep" in m["st"]: m["st"].remove("asleep")
    if m["hp"] <= 0: m["hp"] = 0; out.append("%s hits a %s for %d - it dies." % (who, gr["name"], dmg)); return True
    out.append("%s hits a %s for %d." % (who, gr["name"], dmg)); return False

def hurt_pc(g, c, dmg, out):
    c["hp"] -= dmg
    f = g.get("fight") or {}
    if c["hp"] <= 0 and charm(c, "saint's knuckle") and c["name"] not in f.setdefault("saved", []):
        f["saved"].append(c["name"]); c["hp"] = 1
        out.append("  %s should be down - the saint's knuckle burns, and holds at 1 HP." % c["name"]); return
    down_check(c, out)

def pc_act(g, c, order, out):
    if not can_act(c): return
    act = order[0]; arg = order[1] if len(order) > 1 else None
    if act == "defend": out.append("%s defends." % c["name"]); return
    if act == "potion":
        if g["potions"] <= 0: out.append("%s reaches for a potion - none left." % c["name"]); return
        t = pc(g, arg) if arg else c; g["potions"] -= 1; h = d(2, 8) + 2
        t["hp"] = min(t["maxhp"], t["hp"] + h); t["st"] = [s for s in t["st"] if s not in ("down", "poisoned")]
        out.append("%s gives %s a potion: +%d HP." % (c["name"], "themself" if t is c else t["name"], h)); return
    if act == "attack":
        gr = pick_group(g, arg); swings = 2 if (c["cls"] == "fighter" and c["lvl"] >= 5) else 1
        cleft = False; waff = c.get("waff", "")
        while swings > 0:
            swings -= 1
            if not living_groups(g): return
            live = [m for m in gr["mons"] if m["hp"] > 0]
            if not live: gr = pick_group(g, None); live = [m for m in gr["mons"] if m["hp"] > 0]
            if not live: return
            m = random.choice(live); r = random.randint(1, 20); ac_m = MON[gr["name"]][2]; tags = MON[gr["name"]][8]
            if "asleep" in m["st"]: r = max(r, 15)
            if r + atk_bonus(c) >= ac_m or r == 20:
                n, s_ = WEAPONS[c["wpn"]]; dmg = max(1, d(n, s_) + dmg_bonus(c))
                if waff == "flaming": dmg += d(1, 4)
                if waff == "holy" and "undead" in tags: dmg += d(1, 6)
                if has(c, "smite") and ("undead" in tags or "caster" in tags): dmg += d(1, 6)
                crit = r >= (18 if c["cls"] == "thief" else 20) or (waff == "keen" and r >= 19)
                stab = has(c, "backstab") and c["name"] not in g["fight"].setdefault("stab", [])
                if stab: g["fight"]["stab"].append(c["name"]); crit = True
                if crit: dmg *= 2; out.append("BACKSTAB!" if stab else "CRITICAL!")
                opened = "sleeping" if "asleep" in m["st"] else ("chilled" if "chilled" in m["st"] else "")
                if opened:                                 # one of you set it up, the other finishes
                    dmg = int(dmg * 1.5); out.append("COMBO! (a %s foe)" % opened)
                    if "chilled" in m["st"]: m["st"].remove("chilled")
                if has(c, "venom") and "venom" not in m["st"]: m["st"].append("venom")
                if waff == "vampiric" and c["hp"] < c["maxhp"]: c["hp"] = min(c["maxhp"], c["hp"] + 2)
                if hit_mon(g, gr, m, dmg, out, c["name"]):
                    c["kills"] += 1
                    if has(c, "cleave") and not cleft and living_groups(g): cleft = True; swings += 1; out.append("  %s cleaves on!" % c["name"])
            else: out.append("%s swings at a %s - miss." % (c["name"], gr["name"]))
        return
    if act == "cast":
        sp = arg.lower(); ok, cost, kind, power = SPELLS[sp]
        if c["mp"] < cost: out.append("%s has no MP for %s." % (c["name"], sp)); return
        c["mp"] -= cost; lv = c["lvl"]; tgt = order[2] if len(order) > 2 else None
        mb = max(0, bonus(c["s"][CLS[c["cls"]]["mp"]]))
        sb = (2 if has(c, "focus") else 0) + (2 if charm(c, "ember stone") else 0)
        out.append("%s casts %s." % (c["name"], sp.upper()))
        if kind in ("hit1", "hitgroup", "hitall"):
            if not living_groups(g): return
            grs = living_groups(g) if kind == "hitall" else [pick_group(g, tgt)]
            for gr in grs:
                ms = [m for m in gr["mons"] if m["hp"] > 0]
                if kind == "hit1": ms = random.sample(ms, min(len(ms), 2 if (has(c, "chain") and sp == "spark") else 1))
                for m in ms:
                    dead = hit_mon(g, gr, m, dice(power) + lv // 2 + mb + sb, out, "  " + sp.capitalize())
                    if not dead and sp == "flame": m["burn"] = 2
                    if not dead and sp == "frost" and "chilled" not in m["st"]: m["st"].append("chilled")
            if sp == "flame": out.append("  (what's left of them is burning: 2 a round for two rounds)")
            if sp == "frost": out.append("  (survivors are CHILLED: -2 to hit, and the next blade that finds one is a COMBO)")
        elif kind == "sleep":
            if not living_groups(g): return
            gr = pick_group(g, tgt)
            if "undead" in MON[gr["name"]][8]: out.append("  The %ss do not sleep." % gr["name"]); return
            n = 0; deep = has(c, "deepsleep")
            for m in gr["mons"]:
                if m["hp"] > 0 and random.randint(1, 20) + MON[gr["name"]][0] * 2 < 12 + lv + (3 if deep else 0):
                    m["st"].append("asleep"); n += 1
                    if deep: m["deep"] = True
            out.append("  %d %s fall asleep.%s" % (n, gr["name"] + "s", " (hit a sleeper for a COMBO)" if n else ""))
        elif kind in ("heal1", "healall"):
            for t in ([pc(g, tgt)] if (kind == "heal1" and tgt) else ([c] if kind == "heal1" else g["party"])):
                h = dice(power) + lv + mb + (3 if has(c, "blessing") else 0)
                if has(c, "mercy"): h = int(h * 1.5)
                t["hp"] = min(t["maxhp"], t["hp"] + h); t["st"] = [s for s in t["st"] if s != "down"]
                if has(c, "sanctuary"): t["st"] = [s for s in t["st"] if s not in ("poisoned", "asleep", "paralyzed")]
                out.append("  %s +%d HP." % (t["name"], h))
        elif kind == "ward":
            g["fight"]["ward"] = 3 if has(c, "censure") else True
            out.append("  A ward shimmers (+%d AC this fight)." % (3 if has(c, "censure") else 2))
        elif kind == "cure":
            t = pc(g, tgt) if tgt else c; t["st"] = [s for s in t["st"] if s not in ("poisoned", "asleep", "paralyzed")]; out.append("  %s is cured." % t["name"])
        elif kind == "banish":
            if not living_groups(g): return
            gr = pick_group(g, tgt)
            if "undead" not in MON[gr["name"]][8]: out.append("  Nothing happens - the %ss aren't undead." % gr["name"]); return
            for m in gr["mons"]:
                if m["hp"] > 0 and random.randint(1, 20) < 9 + lv * 2 - MON[gr["name"]][0] * 2: m["hp"] = 0; out.append("  a %s crumbles to dust." % gr["name"])
        elif kind == "raise":
            t = pc(g, tgt) if tgt else c; t["hp"] = max(t["hp"], t["maxhp"] // 2); t["st"] = []; out.append("  %s rises." % t["name"])

def mon_act(g, gr, m, out):
    if m["hp"] <= 0: return
    if "asleep" in m["st"]:
        if random.random() < (.10 if m.get("deep") else .25): m["st"].remove("asleep")
        return
    name, t = gr["name"], MON[gr["name"]]; tags = t[8]; tier = t[0]; df = DF(g); el = bool(m.get("elite"))
    label = ("ELITE " if el else "") + name
    md = lambda x: max(1, int(round(x * df["dmg"])) + (2 if el else 0))
    targets = [c for c in g["party"] if c["hp"] > 0]
    if not targets: return
    def pick():
        w = [(4.0 if ("defending" in c["st"] and has(c, "bulwark")) else 1.0) * (.5 if c.get("aaff") == "shadowed" else 1.0) for c in targets]
        return random.choices(targets, w)[0]
    roll_ = random.random()
    if "breath" in tags and roll_ < .25:
        out.append("The %s BREATHES fire!" % label)
        for c in targets:
            dmg = md(d(tier + 1, 6)); dmg = dmg // 2 if random.randint(1, 20) + bonus(c["s"]["agi"]) >= 14 else dmg
            if has(c, "evasion"): dmg //= 2
            if c.get("aaff") == "warded": dmg = max(1, dmg - 2)
            out.append("  %s -%d" % (c["name"], dmg)); hurt_pc(g, c, dmg, out)
        return
    if "sleeper" in tags and roll_ < .35:
        out.append("The %s chants a sleep spell." % label)
        for c in targets:
            if random.randint(1, 20) + c["lvl"] + bonus(c["s"]["vit"]) < 12 + tier + df["hit"]: c["st"].append("asleep"); out.append("  %s falls asleep." % c["name"])
        return
    if "caster" in tags and roll_ < .4:
        c = pick(); dmg = md(d(1, 6) + tier)
        if c.get("aaff") == "warded": dmg = max(1, dmg - 2)
        out.append("The %s hurls a bolt at %s: -%d." % (label, c["name"], dmg)); hurt_pc(g, c, dmg, out); return
    c = pick()
    r = random.randint(1, 20)
    if any(s in c["st"] for s in ("asleep", "paralyzed")): r = max(r, 14)
    if r + t[3] + df["hit"] + (2 if el else 0) - (2 if "chilled" in m["st"] else 0) >= ac(c, g) or r == 20:
        dmg = md(dice(t[4])); extra = ""
        if "poison" in tags and random.random() < .3 and "poisoned" not in c["st"] and c.get("aaff") != "blessed": c["st"].append("poisoned"); extra = " (poisoned)"
        if "paralyze" in tags and random.random() < .2 and "paralyzed" not in c["st"]: c["st"].append("paralyzed"); extra = " (PARALYZED)"
        out.append("A%s %s hits %s: -%d%s." % ("n" if el else "", label, c["name"], dmg, extra)); hurt_pc(g, c, dmg, out)
        if c.get("aaff") == "thorned":
            m["hp"] -= 2
            if m["hp"] <= 0: m["hp"] = 0; out.append("  ...and dies on %s's thorns." % c["name"])
    else: out.append("A%s %s misses %s." % ("n" if el else "", label, c["name"]))

def win(g):
    f = g["fight"]; df = DF(g)
    xp = int(sum(MON[gr["name"]][5] * (1.5 if m.get("elite") else 1) for gr in f["groups"] for m in gr["mons"]))
    gold = sum(MON[gr["name"]][6] * len(gr["mons"]) for gr in f["groups"]) + random.randint(0, 5) * g["floor"]
    if f["boss"]: xp = int(xp * 1.5); gold *= 2
    if any(charm(c, "lucky coin") for c in g["party"]): gold = int(gold * 1.2)
    if any(has(c, "pockets") for c in g["party"]): gold = int(gold * 1.25)
    share = xp; g["gold"] += gold          # every hero gets the full XP
    print("--\nVICTORY. +%d XP each, +%d gold." % (share, gold)); log(g, "won: +%d xp each, +%d gold" % (share, gold))
    ev = ["VICTORY +%d XP +%d GOLD" % (share, gold)]
    for c in g["party"]:
        c["st"] = [s for s in c["st"] if s in ("poisoned", "down")]
        if c["hp"] <= 0:
            if df["rise"]: c["hp"] = 1; c["st"] = [s for s in c["st"] if s != "down"]; print("%s gets back up (1 HP)." % c["name"])
            else: c["hp"] = 0; c["st"] = ["down"]; print("%s is still DOWN - a heal, a potion, a camp or the inn will bring them back." % c["name"])
        c["xp"] += share; ev += level_check(c)
    for c in g["party"]:
        for l in pick_line(c): print(l)
    if any(pending_picks(c) for c in g["party"]): g["learn_opened"] = now_iso()
    was_boss = f["boss"]; g["fight"] = None; g["calm"] = 2; g["last_event"] = ev; set_pause(g)
    record(g, "kills", sum(len(gr["mons"]) for gr in f["groups"]))
    if was_boss: record(g, "guardians", 1)
    if was_boss and g.get("mode") == "maze":
        floor_of(g)["feat"].pop("%d,%d" % tuple(g["pos"]), None)
    if was_boss and g.get("mode") == "doors": g["doorset"] = []; g["stairs_open"] = True; print("The way down is open: delve.py stairs   (or bank it and go home: delve.py home)")
    if g.get("chest"): print("The chest is still here: delve.py chest open")
    elif random.random() < (1 if was_boss else .3): new_chest(g, "It was guarding a chest."); g["chest"]["rich"] = bool(was_boss)
    print(status_line(g)); save(g); show_next(g)

def wipe(g):
    df = DF(g); record(g, "wipes", 1)
    log(g, "PARTY FELL on floor %d" % g["floor"])
    if df["perma"]:
        print("--\nThe party falls, and on NIGHTMARE that is the end. Floor %d. The dark keeps what it took.\n(delve.py records   |   delve.py new --diff <level>)" % g["floor"])
        g["over"] = True; g["fight"] = None; g["chest"] = None; g["loot"] = None; set_pause(g); save(g); return
    lost = int(g["gold"] * df["lose"]); g["gold"] -= lost
    bag = ""
    if df["lose"] >= 1 and g.get("bag"): bag = " and everything in the bag"; g["bag"] = []
    print("--\nThe party falls. ...You wake in town, bruised, lighter by %d gold%s." % (lost, bag))
    g["fight"] = None; g["chest"] = None; g["loot"] = None; g["shrine"] = None; g["floor"] = 0; g["lost"] = g.get("lost", 0) + 1; g["town_at"] = now_iso(); g["stock"] = None
    for c in g["party"]: c["hp"] = max(1, c["maxhp"] // 4); c["st"] = []; c.pop("bless", None); c.pop("curse", None)
    set_pause(g); save(g)

PERKS = {   # non-spell unlocks, by class and level - shown when reached, and in `sheet` / `guide`
 ("fighter", 5): "SECOND SWING - attacks twice every round",
 ("thief", 3): "SHARPER EYE - better at spotting traps and reading chests (every level adds a little)",
}
def unlocks(cls, lvl):
    out = ["%s - %s" % (n.upper(), SPELL_TXT[n]) for n, sp in SPELLS.items() if sp[0].get(cls) == lvl]
    if (cls, lvl) in PERKS: out.append(PERKS[(cls, lvl)])
    return out
def next_unlock(cls, lvl):
    for L in range(lvl + 1, 13):
        u = unlocks(cls, L)
        if u: return "L%d: %s" % (L, u[0].split(" - ")[0])
    return None

def level_check(c):
    ups = []
    while c["lvl"] < len(XP_AT) and c["xp"] >= XP_AT[c["lvl"]]:
        c["lvl"] += 1; gain = CLS[c["cls"]]["hd"] // 2 + 1 + max(0, bonus(c["s"]["vit"])) + random.randint(0, 3)
        c["maxhp"] += gain; c["hp"] += gain; st = random.choice(STATS); c["s"][st] += 1
        c["maxmp"] = mp_for(c); c["mp"] = c["maxmp"]
        print("*** LEVEL UP! %s is level %d: +%d max HP, +1 %s%s, attack now %+d." % (c["name"], c["lvl"], gain, st.upper(),
              (", MP %d" % c["maxmp"]) if c["maxmp"] else "", atk_bonus(c)))
        ups.append("%s LEVEL %d!" % (c["name"], c["lvl"]))
        for u in unlocks(c["cls"], c["lvl"]): print("    NEW: " + u); ups.append("NEW " + u.split(" - ")[0])
        nx = next_unlock(c["cls"], c["lvl"])
        if nx: print("    next unlock at " + nx)
        if c["lvl"] in TALENTS.get(c["cls"], {}): ups.append("%s: PICK A TALENT" % c["name"])
    return ups

def cmd_run(g):
    f = g.get("fight") or die("No fight on.")
    if f["boss"]: chance = 0 if DF(g)["perma"] else .25
    elif any(charm(c, "boots of flight") for c in alive(g)): chance = 1
    else: chance = .5 + sum(bonus(c["s"]["agi"]) for c in alive(g)) * .04 - .05 * DF(g)["hit"]
    if random.random() < chance:
        g["fight"] = None; g["calm"] = 1
        if g.get("mode") == "maze" and g.get("came_from"): g["pos"] = g["came_from"]
        print("You get away."); set_pause(g); save(g); return
    print("You can't shake them!"); f["orders"] = {}; resolve_round(g, monsters_only=True)

# ---------------------------------------------------------------- chests ---
TRAPS = ["none", "none", "needle", "gas", "blast", "alarm"]
TRAP_TXT = {"none": "no trap", "needle": "poison needle", "gas": "gas bomb", "blast": "exploding box", "alarm": "alarm"}
def new_chest(g, why="A chest."):
    g["chest"] = {"trap": random.choice(TRAPS), "guess": None}
    print("%s  delve.py chest inspect <Name> | disarm <Name> | open | leave" % why)

def spring(g, trap):
    out = []
    if trap == "needle":
        c = random.choice(alive(g)); c["hp"] -= d(1, 4); c["st"].append("poisoned"); out.append("A needle! %s is poisoned." % c["name"]); down_check(c, out)
    elif trap == "gas":
        for c in alive(g):
            if "poisoned" not in c["st"] and c.get("aaff") != "blessed": c["st"].append("poisoned")
        out.append("Gas! Everyone is poisoned.")
    elif trap == "blast":
        for c in alive(g):
            dm = max(1, int((d(2, 6) + g["floor"]) * DF(g)["dmg"])) // (2 if has(c, "evasion") else 1); c["hp"] -= dm; out.append("BOOM - %s -%d." % (c["name"], dm)); down_check(c, out)
    print("\n".join(out))
    if not [c for c in g["party"] if c["hp"] > 0]: wipe(g); return False
    if trap == "alarm":
        print("An alarm shrieks!"); g["chest"]["trap"] = "none"; start_fight(g, encounter(g["floor"], g=g), why="the chest's alarm"); return False
    return True

def loot(g):
    ch = g.get("chest") or {}
    gold = d(2, 10) * g["floor"] + random.randint(5, 20)
    if ch.get("rich"): gold *= 2
    g["gold"] += gold; got = ["%d gold" % gold]
    if random.random() < .35: n = random.randint(1, 2); g["potions"] += n; got.append("%d potion%s" % (n, "s" if n > 1 else ""))
    item = gen_item(g, good=bool(ch.get("rich"))) if (ch.get("rich") or random.random() < .32) else None
    g["chest"] = None; print("Inside: " + ", ".join(got) + (", and something better." if item else ".")); log(g, "chest: " + ", ".join(got + ([item] if item else [])))
    g["last_event"] = (g.get("last_event") or [])[-2:] + ["CHEST: " + got[0]]
    if item: start_loot(g, item)

def loot_text(g):
    lt = g["loot"]; item = lt["item"]; slot = item_parse(item)[0]; L = ["LOOT: %s" % item_desc(item)]
    for c in g["party"]:
        why = can_use(c, item); said = "NEEDS it" if c["name"] in lt["need"] else ("passes" if c["name"] in lt["pass"] else "hasn't said")
        L.append("  %s (%s) now has: %s%s  - %s" % (c["name"], c["owner"], worn(c, slot) or "no charm", ("  [can't use it: %s]" % why) if why else "", said))
    L.append("Each hero: `loot need <Name>` to roll for it, or `loot pass <Name>` to let the others have it. If two need it, the dice decide. If nobody does, it goes in the shared bag.")
    return "\n".join(L)

def start_loot(g, item):
    g["loot"] = {"item": item, "need": [], "pass": []}; g["loot_opened"] = now_iso(); set_pause(g)
    print(loot_text(g))

def cmd_loot(g, a):
    lt = g.get("loot") or die("No loot waiting.")
    if not a: print(loot_text(g)); return
    v = a[0].lower()
    if v not in ("need", "pass") or len(a) < 2: die("loot need <Name> | loot pass <Name>")
    c = pc(g, a[1]); needs_word(g, c["owner"], "loot_opened", "say on the loot")
    if v == "need" and can_use(c, lt["item"]): die("%s can't use it (%s). loot pass %s" % (c["name"], can_use(c, lt["item"]), c["name"]))
    lt["need"] = [n for n in lt["need"] if n != c["name"]]; lt["pass"] = [n for n in lt["pass"] if n != c["name"]]
    lt[v].append(c["name"])
    waiting = [x for x in g["party"] if x["name"] not in lt["need"] + lt["pass"]]
    if waiting:
        save(g); print("%s %s. Waiting on %s." % (c["name"], "needs it" if v == "need" else "passes", ", ".join("%s (%s)" % (x["name"], x["owner"]) for x in waiting))); return
    item = lt["item"]
    if not lt["need"]:
        g.setdefault("bag", []).append(item); print("Nobody needs the %s - into the shared bag (sell it in town, or `equip <Name> %s` later)." % (item, item))
    else:
        if len(lt["need"]) == 1: w = lt["need"][0]
        else:
            while True:
                rolls = {n: random.randint(1, 20) + max(0, bonus(pc(g, n)["s"]["luk"])) for n in lt["need"]}
                print("They both want it. Dice: " + ", ".join("%s %d" % kv for kv in rolls.items()))
                top = max(rolls.values()); best = [n for n, r in rolls.items() if r == top]
                if len(best) == 1: w = best[0]; break
                print("A tie - again.")
        hero = pc(g, w); old = worn(hero, item_parse(item)[0]); put_on(g, hero, item)
        print("%s takes the %s%s." % (w, item, (" (the old %s goes in the bag)" % old) if old else "")); log(g, "%s took %s" % (w, item))
    g["loot"] = None; after_loot(g)

def after_loot(g):
    if g.get("loot") or g.get("shrine"): save(g); return
    next_call_print(g); save(g); show_next(g)

def cmd_shrine(g, a):
    if not g.get("shrine"): die("No shrine here.")
    v = (a[0] if a else "").lower()
    if v == "leave": g["shrine"] = None; print("You leave the candles burning."); after_loot(g); return
    if v != "pray" or len(a) < 2: die("shrine pray <Name> | shrine leave")
    c = pc(g, a[1]); needs_word(g, c["owner"], "pause", "choice at the shrine")
    r = random.randint(1, 20) + max(0, bonus(c["s"]["pie"])) + (2 if c["cls"] in ("priest", "paladin") else 0)
    if r >= 15: c["bless"] = True; print("%s kneels. Warmth. BLESSED: +1 to hit and damage until you leave this floor." % c["name"])
    elif r >= 8:
        for x in g["party"]:
            if x["hp"] > 0: x["hp"] = min(x["maxhp"], x["hp"] + x["maxhp"] // 3)
        c["mp"] = c["maxmp"]; print("%s kneels. The candles steady: the party mends a little, and %s's MP is full." % (c["name"], c["name"]))
    else: c["curse"] = True; print("%s kneels. The candles gutter. CURSED: -2 AC until you leave this floor." % c["name"])
    log(g, "shrine: %s prayed" % c["name"]); g["shrine"] = None; after_loot(g)

def cmd_chest(g, a):
    ch = g.get("chest") or die("No chest here.")
    v = a[0] if a else ""
    if v == "leave": g["chest"] = None; save(g); print("You leave it."); show_next(g); return
    if v == "inspect":
        c = pc(g, a[1]) if len(a) > 1 else die("chest inspect <Name>")
        if c["cls"] != "thief" and random.random() < .5:
            save(g); print("%s squints at it and honestly can't tell. (A thief would know better.)" % c["name"]); return
        good = .6 + bonus(c["s"]["agi"]) * .05 + c["lvl"] * .03 if c["cls"] == "thief" else .6
        guess = ch["trap"] if random.random() < good else random.choice([t for t in TRAPS if t != ch["trap"]])
        ch["guess"] = guess; save(g); print("%s studies it: %s %s." % (c["name"], "pretty sure it's" if c["cls"] == "thief" else "maybe", TRAP_TXT[guess])); return
    if v == "disarm":
        c = pc(g, a[1]) if len(a) > 1 else die("chest disarm <Name>")
        good = .5 + c["lvl"] * .05 + bonus(c["s"]["agi"]) * .05 if c["cls"] == "thief" else .2
        if ch["trap"] == "none": print("%s finds nothing to disarm - it opens." % c["name"])
        elif random.random() < good: print("%s disarms the %s." % (c["name"], TRAP_TXT[ch["trap"]]))
        else:
            print("%s fumbles it!" % c["name"]);
            if not spring(g, ch["trap"]): save(g); return
        loot(g); after_loot(g); return
    if v == "open":
        if ch["trap"] != "none" and not spring(g, ch["trap"]): save(g); return
        loot(g); after_loot(g); return
    die("chest inspect <Name> | disarm <Name> | open | leave")

def next_call_print(g):
    if g.get("fight") or g.get("chest") or g.get("loot") or g.get("shrine"): return
    print("Next call: %s." % next_call(g))

# ------------------------------------------------------------- exploring ---
def in_dungeon(g):
    if g["floor"] == 0: die("You're in town. delve.py go  to descend (or town / inn / shop).")
    if g.get("fight"): die("You're in a fight! delve.py fight")
    if g.get("chest"): die("Deal with the chest first: chest open | inspect <Name> | disarm <Name> | leave")
    if g.get("loot"): die("Decide the loot first:\n" + loot_text(g))
    if g.get("shrine"): die("The shrine first: shrine pray <Name> | shrine leave")

def random_fight(g, trodden=False):
    """A wandering fight. Ground you've already walked is much quieter - going back to the stairs isn't a gauntlet."""
    if g.get("calm", 0) > 0: g["calm"] -= 1; return False
    if random.random() < (.07 + .01 * g["floor"]) * (.3 if trodden else 1):
        start_fight(g, encounter(g["floor"], g=g)); return True
    return False

def event(g, kind):
    """Shared by the maze and the doors. Returns True if it stops movement."""
    if kind == "chest": new_chest(g, "A chest sits in the dust."); return True
    if kind == "trap":
        th = [c for c in alive(g) if c["cls"] == "thief"]
        if th and random.random() < .5 + th[0]["lvl"] * .05: print("%s spots a trap in the floor and you step around it." % th[0]["name"]); return False
        out = []
        if random.random() < .5:
            for c in alive(g): dm = max(1, int((d(1, 6) + g["floor"]) * DF(g)["dmg"])) // (2 if has(c, "evasion") else 1); c["hp"] -= dm; out.append("%s -%d" % (c["name"], dm)); down_check(c, out)
            print("A PIT opens! " + ", ".join(out))
        else:
            c = random.choice(alive(g)); c["hp"] -= d(1, 4); c["st"].append("poisoned"); print("A dart from the wall - %s is poisoned." % c["name"]); down_check(c, out)
        if not [c for c in g["party"] if c["hp"] > 0]: wipe(g); return True
        for c in g["party"]:
            if c["hp"] <= 0 and DF(g)["rise"]: c["hp"] = 1; c["st"] = [s for s in c["st"] if s != "down"]
        return True
    if kind == "fountain":
        r = random.random()
        if r < .6:
            for c in g["party"]: c["hp"] = min(c["maxhp"], c["hp"] + int(c["maxhp"] * .5 * DF(g)["heal"])); c["st"] = [s for s in c["st"] if s not in ("poisoned", "down")]
            print("A clear fountain. You drink: wounds close, poison clears.")
        elif r < .8:
            c = random.choice(alive(g)); c["st"].append("poisoned"); print("A fountain - foul. %s is poisoned." % c["name"])
        else:
            for c in g["party"]:
                if c["maxmp"]: c["mp"] = c["maxmp"]
            print("A fountain that hums. Spell power returns.")
        return True
    if kind == "rest":
        for c in g["party"]:
            if c["hp"] > 0: c["hp"] = min(c["maxhp"], c["hp"] + int(c["maxhp"] / 3 * DF(g)["heal"]))
        print("A quiet alcove. You catch your breath."); return True
    if kind == "merchant":
        print("A hooded peddler: potions %d gold each. (delve.py buy potion)  You have %d gold." % (price(g, 25), g["gold"])); g["peddler"] = True; return True
    if kind == "shrine":
        g["shrine"] = True; set_pause(g)
        print("A small shrine, candles still lit. One of you may pray - a blessing, a mending, or a curse.  shrine pray <Name> | shrine leave"); return True
    if kind == "guardian":
        start_fight(g, encounter(g["floor"], boss=True, g=g), boss=True, why="something guards the stairs"); return True
    if kind == "empty": print(random.choice(["Dust and old bones.", "An empty room.", "Nothing but cold air.", "Scratches on the walls. Nothing else."])); return False
    return False

def cmd_go(g):
    if g["floor"] != 0: die("You're already down there (floor %d)." % g["floor"])
    if len(g["party"]) < 1: die("Hire your heroes first: delve.py hire <HeroName> <class> <WhoPlaysIt>")
    g["floor"] = 1; g["descended"] = True; g["peddler"] = False; g["cart"] = None; record(g, "deepest", 1)
    if g.get("leader") not in [c["owner"] for c in g["party"]]: g["leader"] = g["party"][0]["owner"]
    g["call_opened"] = now_iso()                    # the first call is a call too
    if g["mode"] == "maze":
        if "1" not in g["floors"]: g["floors"]["1"] = gen_floor(1)
        g["pos"] = [0, 0]; g["facing"] = S if not floor_of(g)["walls"][0][0] & S else E
        print("You descend into the dark. Floor 1."); print(ascii_map(g))
    else:
        g["floor"] = g.get("resume", 1); g["doorset"] = []; g["step"] = 0; g["stairs_open"] = False
        print("You descend%s. Floor %d - %d doors to the stairs." % (" by the shortcut" if g["floor"] > 1 else "", g["floor"], DOOR_STEPS))
    picture(g, "floor"); print("First call: %s." % g.get("leader", "?")); save(g)

def cmd_look(g, img):
    in_dungeon(g)
    if g["mode"] == "doors": return cmd_doors(g, img)
    f = floor_of(g); k = "%d,%d" % tuple(g["pos"]); ft = f["feat"].get(k)
    here = {"down": " Stairs down are here (delve.py stairs).", "up": " Stairs up are here (delve.py stairs).", "chest": "", "fountain": " A dry fountain."}.get(ft, "") if ft != "trap" else ""
    print("Floor %d. %s%s" % (g["floor"], ahead_text(g), here)); print("Call: %s." % g.get("leader", "?"))
    if img: print("image: " + with_hud(g, draw_view(g), False).save(os.path.join(OUT, "view.png")))

def cmd_turn(g, a):
    in_dungeon(g)
    v = (a[0] if a else "").lower()[:1]
    g["facing"] = {"l": LEFT[g["facing"]], "r": RIGHT[g["facing"]], "b": OPP[g["facing"]]}.get(v) or die("turn l|r|back")
    save(g); print(ahead_text(g))

def cmd_move(g, a, img):
    in_dungeon(g)
    if g["mode"] == "doors": die("Doors mode: delve.py doors / pick 1|2|3")
    needs_word(g, g.get("leader", "?"), "call_opened", "call (which way)")
    v = (a[0] if a else "f").lower(); steps = min(6, int(a[1])) if len(a) > 1 and a[1].isdigit() else 1
    rel = {"f": 0, "forward": 0, "b": 2, "back": 2, "l": 3, "left": 3, "r": 1, "right": 1}
    if v in DIRS: dr = DIRS[v]
    elif v in rel:
        dr = g["facing"]
        for _ in range(rel[v]): dr = RIGHT[dr]
    else: die("move f|b|l|r or n|s|e|w [steps]")
    g["facing"] = dr; f = floor_of(g); moved = 0; g["peddler"] = False
    for _ in range(steps):
        x, y = g["pos"]
        if f["walls"][y][x] & dr: print("A wall." if moved == 0 else "Wall - you stop."); break
        g["came_from"] = [x, y]; g["pos"] = [x + DX[dr][0], y + DX[dr][1]]; moved += 1
        k = "%d,%d" % tuple(g["pos"]); trod = k in f["seen"]
        if not trod: f["seen"].append(k)
        ft = f["feat"].get(k)
        if ft in ("chest", "trap", "fountain", "guardian", "shrine"):
            if ft != "guardian": f["feat"][k] = "spent" if ft != "fountain" else "dryfountain"
            if event(g, ft): break
        elif ft in ("down", "up"): print("Stairs %s." % ("down" if ft == "down" else "up")); break
        if random_fight(g, trod): break
    if g["floor"] == 0: save(g); return                     # the party fell (a trap) and woke in town
    if moved: print("Moved %d. %s" % (moved, ahead_text(g)))
    if img and not g.get("fight"): print("image: " + with_hud(g, draw_view(g), False).save(os.path.join(OUT, "view.png")))
    if img and g.get("fight"): print("image: " + draw_fight(g))
    if moved: next_call_print(g)
    save(g)

def cmd_stairs(g):
    in_dungeon(g)
    if g["mode"] == "doors":
        if not g.get("stairs_open"): die("No stairs yet - %d more door(s), then the guardian." % max(0, DOOR_STEPS - g["step"]))
        g["floor"] += 1; g["step"] = 0; g["doorset"] = []; g["stairs_open"] = False; g["peddler"] = False
        for c in g["party"]: c.pop("bless", None); c.pop("curse", None)
        g["deepest"] = max(g.get("deepest", 1), g["floor"]); record(g, "deepest", g["floor"])
        print("Down to floor %d. It's colder here." % g["floor"]); log(g, "reached floor %d" % g["floor"]); picture(g, "floor"); next_call_print(g); save(g); return
    ft = floor_of(g)["feat"].get("%d,%d" % tuple(g["pos"]))
    if ft == "down":
        g["floor"] += 1
        if str(g["floor"]) not in g["floors"]: g["floors"][str(g["floor"])] = gen_floor(g["floor"])
        g["pos"] = [0, 0]; f = floor_of(g); g["facing"] = S if not f["walls"][0][0] & S else E
        for c in g["party"]: c.pop("bless", None); c.pop("curse", None)
        g["deepest"] = max(g.get("deepest", 1), g["floor"]); record(g, "deepest", g["floor"])
        print("Down to floor %d." % g["floor"]); log(g, "reached floor %d" % g["floor"]); print(ascii_map(g)); picture(g, "floor")
    elif ft == "up":
        if g["floor"] == 1:
            g["floor"] = 0; g["town_at"] = now_iso(); g["stock"] = None
            for c in g["party"]: c.pop("bless", None); c.pop("curse", None)
            print("Up into daylight. Town: delve.py town"); save(g); return
        g["floor"] -= 1; f = floor_of(g)
        down = [k for k, v in f["feat"].items() if v == "down"][0]; g["pos"] = list(map(int, down.split(",")))
        print("Up to floor %d." % g["floor"]); print(ahead_text(g))
    else: die("No stairs here.")
    next_call_print(g); save(g)

def cmd_map(g, img):
    if g["floor"] == 0: die("You're in town.")
    if g["mode"] == "doors":
        bar = "".join("#" if i < g["step"] else "." for i in range(DOOR_STEPS))
        print("Floor %d  [%s] %d/%d doors%s" % (g["floor"], bar, g["step"], DOOR_STEPS, " - stairs open" if g.get("stairs_open") else "")); return
    print(ascii_map(g))
    if img: print("image: " + draw_map(g))

# ---------------------------------------------------------------- doors ---
DOOR_STEPS = 7
HINTS = {"fight": ["claw marks on the frame", "something breathing behind it", "the smell of wet fur", "a broken spear by the step"],
         "chest": ["a glint under the door", "the lock is newer than the door", "it's heavier than it should be"],
         "trap": ["the floor in front is too clean", "a thin wire glints", "the hinges are oiled"],
         "fountain": ["the sound of running water", "the air is damp", "moss around the frame"],
         "rest": ["it's quiet", "warm air seeps out", "no tracks in the dust"],
         "merchant": ["someone humming", "a lantern lit behind it", "coins clinking"],
         "shrine": ["candle smoke", "a low humming", "wax on the threshold"],
         "empty": ["nothing special", "an ordinary door", "dust"]}
DWEIGHT = [("fight", 40), ("chest", 14), ("trap", 10), ("fountain", 7), ("rest", 9), ("merchant", 5), ("shrine", 6), ("empty", 12)]
def new_doors(g=None):
    """Three doors that are never three of a kind, with three different hints (a hint is honest 7 times in 10)."""
    fl = (g or {}).get("floor", 1); w = dict(DWEIGHT); w["trap"] += min(8, fl); w["fight"] += min(12, 2 * fl)
    for _ in range(30):
        kinds = random.choices(list(w), list(w.values()), k=3)
        if len(set(kinds)) >= 2 and kinds.count("empty") <= 1: break
    ds, used = [], set()
    for k in kinds:
        for _ in range(12):
            hint_from = k if random.random() < .7 else random.choice(list(HINTS))
            h = random.choice(HINTS[hint_from])
            if h not in used: break
        used.add(h); ds.append({"kind": k, "hint": h})
    return ds

def cmd_doors(g, img=False):
    in_dungeon(g)
    if g.get("stairs_open"): print("The stairs down are open: delve.py stairs"); return
    if g["step"] >= DOOR_STEPS: print("A great door. Something waits beyond it. delve.py pick 1"); return
    if not g.get("doorset"): g["doorset"] = new_doors(g); save(g)
    print("Floor %d, door %d of %d. Call: %s." % (g["floor"], g["step"] + 1, DOOR_STEPS, g.get("leader", "?")))
    for i, dd in enumerate(g["doorset"]): print("  [%d] %s" % (i + 1, dd["hint"]))
    if img: print("image: " + draw_doors(g))

def cmd_pick(g, a):
    in_dungeon(g)
    if g["mode"] != "doors": die("Maze mode: delve.py move ...")
    if g.get("stairs_open"): die("The stairs are open: delve.py stairs")
    if g["step"] >= DOOR_STEPS:
        g["step"] += 1; start_fight(g, encounter(g["floor"], boss=True, g=g), boss=True, why="the floor's guardian"); save(g); return
    needs_word(g, g.get("leader", "?"), "call_opened", "call (which door)")
    if not g.get("doorset"): g["doorset"] = new_doors(g)
    i = int(a[0]) - 1 if a and a[0].isdigit() else die("pick 1|2|3")
    dd = g["doorset"][i]; g["doorset"] = []; g["step"] += 1; g["peddler"] = False
    print("Door %d..." % (i + 1))
    if dd["kind"] == "fight": start_fight(g, encounter(g["floor"], g=g))
    else:
        event(g, dd["kind"])
        if dd["kind"] != "empty": set_pause(g)
    if g["step"] >= DOOR_STEPS and not g.get("fight"): print("Ahead: the guardian's door. (pick 1 when ready)")
    next_call_print(g); save(g)
    show_next(g)

def show_next(g):
    """In doors mode, when nothing is pending, put the next three doors on the table in the same output."""
    if g.get("mode") == "doors" and g["floor"] and not g.get("fight") and not g.get("chest") and not g.get("loot") and not g.get("shrine") and not g.get("stairs_open") and g["step"] < DOOR_STEPS:
        if not g.get("doorset"): g["doorset"] = new_doors(g); save(g)
        print("Next doors (call: %s): " % g.get("leader", "?") + "  ".join("[%d] %s" % (i + 1, dd["hint"]) for i, dd in enumerate(g["doorset"])))
        picture(g, "doors"); g["last_event"] = []; save(g)

def draw_doors(g):
    cv = Canvas(96, 64, (12, 10, 16)); cv.rect(0, 40, 96, 64, (26, 22, 18))
    for i in range(3):
        x0 = 10 + i * 30; cv.rect(x0, 14, x0 + 18, 44, (90, 60, 35)); cv.rect(x0 + 2, 16, x0 + 16, 44, (120, 80, 45))
        cv.rect(x0 + 13, 30, x0 + 15, 32, (220, 200, 90))
        for k in range(i + 1): cv.rect(x0 + 4 + k * 4, 8, x0 + 6 + k * 4, 11, (230, 230, 230))
    return with_hud(g, cv, False).save(os.path.join(OUT, "doors.png"))

# ---------------------------------------------------------- camp / magic ---
def cmd_camp(g):
    in_dungeon(g)
    if g.get("camped") == "%d:%d" % (g["floor"], g.get("step", 0) if g["mode"] == "doors" else len(floor_of(g)["seen"]) // 6):
        die("You've just camped - move on a little first.")
    g["camped"] = "%d:%d" % (g["floor"], g.get("step", 0) if g["mode"] == "doors" else len(floor_of(g)["seen"]) // 6)
    if random.random() < DF(g)["amb"]: print("You settle in... and something finds you."); start_fight(g, encounter(g["floor"], g=g)); save(g); return
    for c in g["party"]:
        c["hp"] = min(c["maxhp"], max(1, c["hp"]) + int(c["maxhp"] * .4 * DF(g)["heal"])); c["mp"] = min(c["maxmp"], c["mp"] + int(c["maxmp"] * .4 * DF(g)["heal"]) + 1)
        c["st"] = [s for s in c["st"] if s not in ("down", "asleep", "paralyzed")]
    print("You rest a while, undisturbed.  " + status_line(g)); save(g)

def cmd_use(g, a):
    if g.get("fight"): die("In a fight, use: do <Name> potion [ally]")
    if len(a) < 2 or a[1] != "potion": die("use <Name> potion [ally]")
    if g["potions"] <= 0: die("No potions.")
    t = pc(g, a[2]) if len(a) > 2 else pc(g, a[0]); g["potions"] -= 1; h = d(2, 8) + 2
    t["hp"] = min(t["maxhp"], t["hp"] + h); t["st"] = [s for s in t["st"] if s not in ("down", "poisoned")]
    print("%s drinks: +%d HP (%d/%d). %d potions left." % (t["name"], h, t["hp"], t["maxhp"], g["potions"])); save(g)

def cmd_cast_out(g, a):
    if g.get("fight"): die("In a fight, use: do <Name> cast <spell> ...")
    if len(a) < 2: die("cast <Name> <spell> [ally]")
    c = pc(g, a[0]); sp = a[1].lower()
    if sp not in known(c): die("%s knows: %s" % (c["name"], ", ".join(known(c)) or "nothing yet"))
    kind = SPELLS[sp][2]
    if kind not in ("heal1", "healall", "cure", "raise"): die("Only healing magic works outside a fight.")
    if c["mp"] < SPELLS[sp][1]: die("Not enough MP.")
    out = []; g["fight"] = {"ward": False}; pc_act(g, c, ["cast", sp] + a[2:], out); g["fight"] = None
    print("\n".join(out)); save(g)

# ------------------------------------------------------------------ town ---
SHOP = {"potion": 15, "leather": 20, "mail": 60, "plate": 220, "axe": 60, "greatsword": 140, "sword": 30, "mace": 20}
def in_town(g):
    if g["floor"] != 0 and not (g.get("peddler") and True): die("You're in the dungeon (floor %d). Stairs up on floor 1 lead home." % g["floor"])

def cmd_town(g):
    if g["floor"] != 0: die("You're on floor %d." % g["floor"])
    print("TOWN. Gold %d, potions %d.  inn (heal all: %d gold) | shop | equip <Name> <item> | go (descend)" % (g["gold"], g["potions"], inn_cost(g)))
    print(status_line(g))

def inn_cost(g): return price(g, sum(2 * c["lvl"] for c in g["party"]))
def cmd_inn(g):
    if g["floor"] != 0: die("No inn down here.")
    cost = inn_cost(g)
    if g["gold"] < cost: die("The inn costs %d; you have %d." % (cost, g["gold"]))
    g["gold"] -= cost
    for c in g["party"]: c["hp"] = c["maxhp"]; c["mp"] = c["maxmp"]; c["st"] = []; c.pop("bless", None); c.pop("curse", None)
    print("A night at the inn (-%d gold). Everyone's whole again." % cost); save(g)

def shop_stock(g):
    if g.get("stock") is None:
        deep = max(1, g.get("resume", 1), g.get("deepest", 1))
        g["stock"] = [gen_item({"floor": deep}, good=True) for _ in range(3)]; save(g)
    return g["stock"]
def shop_price(g, it):
    if it in SHOP: return price(g, SHOP[it])
    if it in shop_stock(g): return price(g, item_value(it) * 2)
    return None
def cart(g):
    if not g.get("cart"): g["cart"] = {"items": [], "agreed": []}
    return g["cart"]
def cart_text(g):
    ct = cart(g); L = []
    if not ct["items"]: return "The cart is empty.  want <Name> <item>"
    for c in g["party"]:
        mine = [x for x in ct["items"] if x["hero"] == c["name"]]
        if mine: L.append("  %s wants: %s" % (c["name"], ", ".join("%s (%d)" % (x["item"], x["price"]) for x in mine)))
    tot = sum(x["price"] for x in ct["items"]); owners = sorted(set(c["owner"] for c in g["party"]))
    L.append("  TOTAL %d of your %d gold%s." % (tot, g["gold"], "" if tot <= g["gold"] else "  - TOO MUCH: someone has to `unwant` something"))
    L.append("  Agreed so far: %s. It is bought when every one of you has agreed (agree <Name>); any change to the cart starts the agreeing over." % (", ".join(ct["agreed"]) or "nobody"))
    return "CART\n" + "\n".join(L)
def cmd_shop(g):
    if g["floor"] != 0 and not g.get("peddler"): die("No shop down here.")
    if g["floor"] != 0: print("The peddler only has potions: %d gold." % price(g, 25)); return
    print("SHOP (gold %d): " % g["gold"] + ", ".join("%s %d" % (k, price(g, v)) for k, v in SHOP.items()))
    print("  weapons: dagger 1d4, staff/mace 1d6, sword 1d8, axe 1d10, greatsword 2d6.  armor AC: leather +2, mail +4, plate +6.")
    print("TODAY'S FINDS (gone when you next come back up):")
    for it in shop_stock(g): print("  %-24s %4d gold  - %s" % (it, shop_price(g, it), item_desc(it).split("(", 1)[1].rstrip(")")))
    if g.get("bag"): print("BAG (shared): " + ", ".join("%s (sells for %d)" % (b, max(1, item_value(b) // 2)) for b in g["bag"]) + "   sell <item> | equip <Name> <item>")
    print("You shop TOGETHER: `want <Name> <item>` puts it in the cart for that hero, `cart` shows it, `unwant <Name> <item>` takes it out,")
    print("and nothing is bought until each of you says `agree <Name>`.")
    if g.get("cart") and g["cart"]["items"]: print(cart_text(g))

def cmd_want(g, a, remove=False):
    if g["floor"] != 0: die("The shop is in town.")
    if len(a) < 2: die("%s <Name> <item>" % ("unwant" if remove else "want"))
    c = pc(g, a[0]); it = " ".join(a[1:]).lower(); ct = cart(g)
    needs_word(g, c["owner"], "town_at", "shopping wish")
    if remove:
        hit = [x for x in ct["items"] if x["hero"] == c["name"] and x["item"] == it] or die("That's not in the cart for %s." % c["name"])
        ct["items"].remove(hit[0])
    else:
        p = shop_price(g, it)
        if p is None: die("Not sold here. delve.py shop")
        if it != "potion" and can_use(c, it): die("%s can't use that (%s)." % (c["name"], can_use(c, it)))
        if it in g.get("stock", []) and any(x["item"] == it for x in ct["items"]): die("There's only one %s, and it's already in the cart." % it)
        ct["items"].append({"hero": c["name"], "item": it, "price": p})
    ct["agreed"] = []; g["cart_changed"] = now_iso(); save(g); print(cart_text(g))

def cmd_agree(g, a):
    if g["floor"] != 0: die("The shop is in town.")
    ct = cart(g)
    if not ct["items"]: die("The cart is empty.  want <Name> <item>")
    c = pc(g, a[0]) if a else die("agree <Name>")
    needs_word(g, c["owner"], "cart_changed", "yes on the cart")
    tot = sum(x["price"] for x in ct["items"])
    if tot > g["gold"]: die("The cart is %d and you have %d. Someone has to `unwant` something." % (tot, g["gold"]))
    if c["owner"] not in ct["agreed"]: ct["agreed"].append(c["owner"])
    owners = sorted(set(x["owner"] for x in g["party"]))
    if [o for o in owners if o not in ct["agreed"]]:
        save(g); print("%s agrees. Waiting on %s." % (c["owner"], ", ".join(o for o in owners if o not in ct["agreed"]))); return
    for x in ct["items"]:
        g["gold"] -= x["price"]
        if x["item"] == "potion": g["potions"] += 1
        else:
            if x["item"] in g.get("stock", []): g["stock"].remove(x["item"])
            put_on(g, pc(g, x["hero"]), x["item"])
    print("Bought: " + ", ".join("%s for %s" % (x["item"], x["hero"]) for x in ct["items"]) + ". Gold %d, potions %d." % (g["gold"], g["potions"]))
    log(g, "bought: " + ", ".join(x["item"] for x in ct["items"])); g["cart"] = None; save(g)

def cmd_sell(g, a):
    if g["floor"] != 0: die("You sell in town.")
    it = " ".join(a).lower()
    if it not in g.get("bag", []): die("Not in the bag. Bag: %s" % (", ".join(g.get("bag", [])) or "empty"))
    for h in HUMANS: needs_word(g, h, "town_at", "say on selling from the shared bag")
    v = max(1, item_value(it) // 2); g["bag"].remove(it); g["gold"] += v; save(g); print("Sold the %s for %d. Gold %d." % (it, v, g["gold"]))

def cmd_buy(g, a):
    if not a: die("buy <item> [Name]")
    it = a[0].lower()
    if g["floor"] != 0:
        if not (g.get("peddler") and it == "potion"): die("Nothing to buy here.")
        p = price(g, 25)
        if g["gold"] < p: die("A potion is %d; you have %d." % (p, g["gold"]))
        g["gold"] -= p; g["potions"] += 1; print("Bought a potion (%d now). Gold %d." % (g["potions"], g["gold"])); save(g); return
    owners = sorted(set(c["owner"] for c in g["party"]))
    if len(owners) > 1: die("In town you shop together: `want <Name> <item>` for each thing, `cart` to see it, then each of you `agree <Name>`. (delve.py shop)")
    who = a[1] if len(a) > 1 else g["party"][0]["name"]
    cmd_want(g, [who] + [" ".join([it] + a[2:])]); cmd_agree(load(), [who])

def cmd_equip(g, a):
    if len(a) < 2: die("equip <Name> <item>   bag: %s" % (", ".join(g.get("bag", [])) or "empty"))
    c = pc(g, a[0]); it = " ".join(a[1:]).lower()
    if it not in g.get("bag", []): die("Not in the bag. Bag: %s" % (", ".join(g.get("bag", [])) or "empty"))
    if can_use(c, it): die("%s can't use that (%s)." % (c["name"], can_use(c, it)))
    needs_word(g, c["owner"], "pause", "change of gear")
    g["bag"].remove(it); put_on(g, c, it)
    print("%s now uses %s. AC %d, attack %+d." % (c["name"], item_desc(it), ac(c), atk_bonus(c))); save(g)

# ------------------------------------------------------------------ setup ---
def cmd_new(a):
    mode = "doors" if "--mode" in a and a[a.index("--mode") + 1] == "doors" else "maze"
    diff = a[a.index("--diff") + 1].lower() if "--diff" in a and len(a) > a.index("--diff") + 1 else "normal"
    if diff not in DIFFS: die("Difficulty: " + ", ".join(DIFF_ORDER))
    if os.path.exists(SAVE) and "--force" not in a:
        old = json.load(open(SAVE))
        if old.get("party"):
            arch = os.path.join(HERE, "saves"); os.makedirs(arch, exist_ok=True)
            os.replace(SAVE, os.path.join(arch, "expedition_%s.json" % time.strftime("%Y%m%d_%H%M")))
            print("(the old expedition is saved in saves/)")
    g = {"mode": mode, "diff": diff, "floor": 0, "party": [], "gold": 60, "potions": 3, "floors": {}, "leader": (HUMANS or ["?"])[0],
         "fight": None, "chest": None, "bag": [], "started": time.strftime("%Y-%m-%d"), "v": 2, "town_at": now_iso()}
    save(g); log(g, "NEW EXPEDITION (%s, %s)" % (mode, diff))
    print("New expedition - %s, difficulty %s (%s)." % ("MAZE (explore a grid; you pick where to walk on the map)" if mode == "maze" else "DOORS (pick a door, go deeper)", diff.upper(), DIFFS[diff]["blurb"]))
    print("One hero each, up to four of you:")
    for k, v in CLS.items(): print("  %-8s %s" % (k, v["blurb"]))
    print("delve.py hire <HeroName> <class> <WhoPlaysIt>      e.g.  hire Brakka fighter Sam")

def cmd_hire(g, a):
    if len(a) < 3: die("hire <HeroName> <class> <WhoPlaysIt>")
    name, cls, owner = a[0], a[1].lower(), a[2].capitalize()
    if cls not in CLS: die("Classes: " + ", ".join(CLS))
    if g.get("descended"): die("The expedition is under way - start a new one to change heroes.")
    if any(c["owner"] == owner for c in g["party"]): die("%s already has a hero (one each)." % owner)
    if len(g["party"]) >= 4: die("Four is a full party.")
    if any(c["name"].lower() == name.lower() for c in g["party"]): die("Name taken.")
    c = roll_char(name, cls, owner); g["party"].append(c); save(g); log(g, "hired %s the %s (%s)" % (name, cls, owner))
    print(sheet(c))
    print("(one reroll allowed: delve.py reroll %s)" % name)
    if len(g["party"]) >= 2: print("Party of %d ready: delve.py go   (up to four can come; foes scale with the party)" % len(g["party"]))

def sheet(c):
    tl = [(k, dsc) for L, pair in TALENTS.get(c["cls"], {}).items() for k, dsc in pair if k in c.get("tal", [])]
    return ("%s - %s, level %d (%s)\n  HP %d  MP %d  AC %d  attack %+d\n  weapon: %s\n  armor:  %s\n  charm:  %s\n  " % (c["name"], c["cls"], c["lvl"], c["owner"], c["maxhp"], c["maxmp"], ac(c), atk_bonus(c),
            item_desc(worn(c, "weapon")), item_desc(worn(c, "armor")), item_desc(c["charm"]) if c.get("charm") else "none")
            + "  ".join("%s %d" % (k.upper(), v) for k, v in c["s"].items()) + (("\n  spells: " + ", ".join(known(c))) if known(c) else "")
            + (("\n  talents: " + "; ".join("%s (%s)" % x for x in tl)) if tl else "") + "".join("\n  " + l for l in pick_line(c)))

def cmd_reroll(g, a):
    c = pc(g, a[0]) if a else die("reroll <Name>")
    if g.get("descended"): die("Too late - you've been down already.")
    if c["rerolled"]: die("%s already used the reroll." % c["name"])
    n = roll_char(c["name"], c["cls"], c["owner"]); n["rerolled"] = True
    g["party"][g["party"].index(c)] = n; save(g); print(sheet(n) + "\n(that's final)")

def cmd_party(g):
    where = "town" if g["floor"] == 0 else "floor %d" % g["floor"]
    print("%s mode, %s. Gold %d, potions %d%s.  Call: %s" % (g["mode"], where, g["gold"], g["potions"],
          (", bag: " + ", ".join(g["bag"])) if g.get("bag") else "", g.get("leader", "?")))
    for c in g["party"]: print("  " + cline(c, g) + "  XP %d/%s" % (c["xp"], XP_AT[c["lvl"]] if c["lvl"] < len(XP_AT) else "max"))

def cmd_sheet(g, a):
    who = [pc(g, a[0])] if a else g["party"]
    for c in who:
        print(sheet(c)); print("  XP %d/%s%s" % (c["xp"], XP_AT[c["lvl"]] if c["lvl"] < len(XP_AT) else "max",
              ("   standing order: " + c["stance"]) if c.get("stance") else ""))
        if known(c): print("  " + "; ".join("%s %dMP - %s" % (sp, SPELLS[sp][1], SPELL_TXT[sp]) for sp in known(c)))
        nxt = [(n, sp[0][c["cls"]]) for n, sp in SPELLS.items() if c["cls"] in sp[0] and sp[0][c["cls"]] > c["lvl"]]
        if nxt: print("  learns later: " + ", ".join("%s (L%d)" % x for x in sorted(nxt, key=lambda x: x[1])))
        pk = [(L, t) for (k, L), t in PERKS.items() if k == c["cls"]]
        if pk: print("  perks: " + "; ".join(("%s (L%d%s)" % (t.split(" - ")[0], L, ", have it" if c["lvl"] >= L else "")) for L, t in pk))
    print("Party: gold %d, potions %d, bag: %s.  Gold buys gear/potions in town (shop) and a night at the inn." % (g["gold"], g["potions"], ", ".join(g.get("bag", [])) or "empty"))

def cmd_guide(g=None):
    print("""DELVE - quick refresher
GOAL   Go down, floor by floor. DOORS: 7 doors, a guardian, then stairs (or `home` to bank the depth).
       MAZE: explore the grid - `map` shows it with letters across and numbers down; `goto C4` walks there.
TURNS  The script says whose CALL it is (which door / which square) and it alternates. In fights each of you orders
       your OWN hero. After a fight or a find, the game waits until everyone has had their say - that's the beat.
FIGHT  attack | defend (+4 AC) | potion (+2d8+2 HP, cures poison; shared) | cast <spell>.  MP returns at camp, fountains, the inn.
       COMBO: a weapon hit on a SLEEPING or CHILLED foe does half again as much. One sets up, the other finishes.
       `stance <Name> auto` = keep attacking this fight until anyone drops below half HP.""")
    for n, sp in SPELLS.items():
        print("       %-6s %dMP  %s  (%s)" % (n.upper(), sp[1], SPELL_TXT[n], ", ".join("%s L%d" % kv for kv in sp[0].items())))
    print("""TALENT at levels 2 and 4 pick one of two: `learn` shows what's waiting. `info talents` lists them all.
LOOT   gear is offered to everyone: `loot need <Name>` or `loot pass <Name>`. Two needs = the dice decide. Each hero wears
       one weapon, one armor, one charm. Affixes matter (keen, vampiric, thorned, warded...): `sheet` explains yours.
TOWN   you shop TOGETHER: `want <Name> <item>` fills a cart, `agree <Name>` from each of you buys it. `sell` what's in the bag.
DIFFICULTY  story < easy < normal < hard < brutal < nightmare (`diff`). From BRUTAL the fallen stay down until healed;
       on NIGHTMARE one wipe ends the expedition. `records` keeps the deepest floor reached on each.
SEE    `board` = right now; `sheet` = stats, gear, talents; `info <spell|class|monster|item|talents>`; `log`.""")
    if g: print("\nRIGHT NOW:\n" + board(g))

def cmd_fight(g, img):
    if not g.get("fight"): die("No fight on.")
    print("Round %d. %s" % (g["fight"]["round"], status_line(g))); print(orders_needed(g))
    if img: print("image: " + draw_fight(g))

SPELL_TXT = {"spark": "one foe: 1d8 (+level/2, +INT)", "sleep": "one group may fall asleep (not undead); sleepers are easy to hit",
 "flame": "one group: 2d6 each", "frost": "one group: 3d8 each", "storm": "every foe: 4d6",
 "heal": "one ally: 1d8 + level (+PIE); gets a downed hero up", "ward": "+2 AC for the rest of this fight",
 "cure": "one ally: ends poison, sleep, paralysis", "banish": "one undead group: each may crumble to dust",
 "mend": "every ally: 1d8 + level", "raise": "a downed ally back to half HP"}
def cmd_info(a):
    q = " ".join(a).lower()
    if q in SPELLS:
        cl, mp, _, _ = SPELLS[q]
        print("%s - %d MP. %s. Learned: %s." % (q.upper(), mp, SPELL_TXT[q], ", ".join("%s L%d" % kv for kv in cl.items()))); return
    if q in CLS: print("%s - %s. HP die %d, attack +%d. Spells: %s" % (q, CLS[q]["blurb"], CLS[q]["hd"], CLS[q]["atk"], ", ".join(n for n, sp in SPELLS.items() if q in sp[0]) or "none")); return
    if q in MON:
        t = MON[q]; print("%s - tier %d, AC %d, attack +%d for %s, %d XP%s" % (q, t[0], t[2], t[3], t[4], t[5], (". " + ", ".join(t[8])) if t[8] else "")); return
    if q in ("spells", ""): [print("%-7s %d MP  %s" % (n, sp[1], SPELL_TXT[n])) for n, sp in SPELLS.items()]; return
    if q in ("talents", "talent"):
        for cl, lv in TALENTS.items():
            for L, pair in lv.items(): print("%-8s L%d: %s" % (cl, L, "  |  ".join("%s - %s" % p for p in pair)))
        return
    for cl, lv in TALENTS.items():
        for L, pair in lv.items():
            for k, dsc in pair:
                if q == k: print("%s (%s, level %d): %s." % (k.upper(), cl, L, dsc)); return
    if q in WAFF: print("%s (weapon affix): %s." % (q, WAFF[q])); return
    if q in AAFF: print("%s (armor affix): %s." % (q, AAFF[q])); return
    if item_parse(q): print(item_desc(q)); return
    if q in ("items", "gear", "affixes", "charms"):
        print("WEAPON affixes: " + "; ".join("%s - %s" % kv for kv in WAFF.items())); print("ARMOR affixes: " + "; ".join("%s - %s" % kv for kv in AAFF.items()))
        print("CHARMS: " + "; ".join("%s - %s" % kv for kv in CHARMS.items())); return
    die("info <spell|class|monster|item|talent>   or: info spells | info talents | info items")

def cmd_learn(g, a):
    if len(a) < 2:
        L = [l for c in g["party"] for l in pick_line(c)]
        print("\n".join(L) if L else "No talent choices waiting. (They come at levels 2 and 4.)"); return
    c = pc(g, a[0]); k = a[1].lower()
    for L in pending_picks(c):
        if k in [x for x, _ in TALENTS[c["cls"]][L]]:
            needs_word(g, c["owner"], "learn_opened", "talent choice")
            c["tal"].append(k); old = c["maxmp"]; c["maxmp"] = mp_for(c); c["mp"] += c["maxmp"] - old
            desc = dict(TALENTS[c["cls"]][L])[k]; save(g); log(g, "%s learned %s" % (c["name"], k))
            print("%s learns %s: %s." % (c["name"], k.upper(), desc)); return
    die("%s can't learn that now. %s" % (c["name"], " ".join(pick_line(c)) or "Nothing is waiting - talents come at levels 2 and 4."))

def cmd_diff(g, a):
    if not a:
        print("Difficulty now: %s" % g.get("diff", "normal").upper())
        for k in DIFF_ORDER:
            v = DIFFS[k]; print("  %-9s foes %d%% HP, %d%% damage, %+d to hit%s%s - %s" % (k, v["hp"] * 100, v["dmg"] * 100, v["hit"],
                  ", elites" if v["elite"] >= .15 else "", ", PERMADEATH" if v["perma"] else "", v["blurb"]))
        print("Change it in town: delve.py diff <level>"); return
    k = a[0].lower()
    if k not in DIFFS: die("Levels: " + ", ".join(DIFF_ORDER))
    if g["floor"] != 0: die("Only in town.")
    for h in HUMANS: needs_word(g, h, "town_at", "say on the difficulty")
    g["diff"] = k; save(g); log(g, "difficulty -> %s" % k); print("Difficulty is now %s: %s." % (k.upper(), DIFFS[k]["blurb"]))

def cmd_records():
    try: r = json.load(open(REC))
    except Exception: r = {}
    if not r: print("No records yet."); return
    print("RECORDS (all expeditions)")
    for k in DIFF_ORDER:
        if k in r: v = r[k]; print("  %-9s deepest floor %d | guardians %d | foes %d | wipes %d" % (k, v.get("deepest", 0), v.get("guardians", 0), v.get("kills", 0), v.get("wipes", 0)))

def cmd_stance(g, a):
    if len(a) < 2: die("stance <Name> attack|defend|off   (a standing order, used whenever that hero has no order)")
    c = pc(g, a[0]); v = a[1].lower()
    if v in ("auto", "smart"): v = "attack-till-hurt"
    if v not in ("attack", "defend", "off", "attack-till-hurt"): die("stance <Name> auto|attack|defend|off   (auto = attack until anyone drops below half HP, then ask)")
    needs_word(g, c["owner"], "opened", "standing order")
    if v == "off": c.pop("stance", None)
    else: c["stance"] = v
    save(g); print("%s: %s." % (c["name"], "no standing order" if v == "off" else ("will attack every round - until anyone drops below half HP, then waits for a real order" if v == "attack-till-hurt" else "will %s every round until told otherwise" % v)))
    if g.get("fight"): try_resolve(g)

def hurt(g): return [c for c in g["party"] if c["hp"] < c["maxhp"] / 2]
def stance_active(g, c):
    st = c.get("stance")
    if st == "attack-till-hurt": return "attack" if not hurt(g) else None
    return st
def try_resolve(g):
    miss = [x for x in g["party"] if can_act(x) and x["name"] not in g["fight"]["orders"] and not stance_active(g, x)]
    if miss: return
    for x in g["party"]:
        if can_act(x) and x["name"] not in g["fight"]["orders"]: g["fight"]["orders"][x["name"]] = [stance_active(g, x)]
    resolve_round(g)

def main():
    a = sys.argv[1:]
    if not a or a[0] in ("help", "-h", "--help"): print(__doc__); return
    v, rest = a[0].lower(), a[1:]
    img = "--img" in rest
    rest = [x for x in rest if x != "--img"]
    if v == "_send":                       # background helper for picture(): post, clean up, log a failure
        try: send_photo(rest[0], open(rest[1], encoding="utf-8").read())
        except Exception as e:
            try: open(os.path.join(OUT, "send.log"), "a").write("%s send failed: %s\n" % (now_iso(), str(e)[:200]))
            except Exception: pass
        for f in rest[:2]:
            try: os.remove(f)
            except Exception: pass
        return
    if v == "new": return cmd_new(rest)
    if v == "info": return cmd_info(rest)
    if v == "guide": return cmd_guide(load() if os.path.exists(SAVE) else None)
    if v == "records": return cmd_records()
    if v == "said":                        # guard "file": mark that this person has just spoken (run by a hook or by the person)
        who = rest[0] if rest else die("said <Name>")
        os.makedirs(os.path.join(HERE, "human_said"), exist_ok=True); p_ = os.path.join(HERE, "human_said", who); open(p_, "a").close(); os.utime(p_, None)
        print("noted: %s has spoken." % who); return
    g = load()
    if g.get("over") and v not in ("party", "sheet", "gear", "log", "board", "diff"): die("This expedition is over (it fell on NIGHTMARE). delve.py records  |  delve.py new [--mode doors|maze] [--diff <level>]")
    if v in ("pick", "move", "goto", "stairs", "chest", "buy", "use", "cast", "camp", "go", "inn", "home", "sell"): needs_turn(g)
    if v == "pics":
        g["pics"] = (rest[:1] != ["off"]); save(g); print("Pictures %s." % ("on (new fights, new floors)" if g["pics"] else "off")); return
    if v == "home":
        if g["mode"] != "doors" or not g.get("stairs_open") or g.get("fight"): die("Home is only open at a beaten floor's stairs (doors mode). Maze: take the stairs up.")
        g["resume"] = g["floor"] + 1; g["floor"] = 0; g["stairs_open"] = False; g["town_at"] = now_iso(); g["stock"] = None
        for c in g["party"]: c.pop("bless", None); c.pop("curse", None)
        print("You climb the long way home. Next time you start at floor %d. delve.py town" % g["resume"]); save(g); return
    T = {"hire": lambda: cmd_hire(g, rest), "reroll": lambda: cmd_reroll(g, rest), "party": lambda: cmd_party(g),
         "go": lambda: cmd_go(g), "look": lambda: cmd_look(g, img), "move": lambda: cmd_move(g, rest, img), "goto": lambda: cmd_goto(g, rest, img),
         "turn": lambda: cmd_turn(g, rest), "map": lambda: cmd_map(g, img), "stairs": lambda: cmd_stairs(g),
         "doors": lambda: cmd_doors(g, img), "pick": lambda: cmd_pick(g, rest), "camp": lambda: cmd_camp(g),
         "use": lambda: cmd_use(g, rest), "cast": lambda: cmd_cast_out(g, rest), "fight": lambda: cmd_fight(g, img),
         "do": lambda: cmd_do(g, rest), "run": lambda: cmd_run(g), "chest": lambda: cmd_chest(g, rest),
         "town": lambda: cmd_town(g), "inn": lambda: cmd_inn(g), "shop": lambda: cmd_shop(g), "buy": lambda: cmd_buy(g, rest),
         "equip": lambda: cmd_equip(g, rest), "stance": lambda: cmd_stance(g, rest), "loot": lambda: cmd_loot(g, rest), "shrine": lambda: cmd_shrine(g, rest),
         "learn": lambda: cmd_learn(g, rest), "want": lambda: cmd_want(g, rest), "unwant": lambda: cmd_want(g, rest, remove=True), "cart": lambda: print(cart_text(g)),
         "agree": lambda: cmd_agree(g, rest), "sell": lambda: cmd_sell(g, rest), "diff": lambda: cmd_diff(g, rest), "difficulty": lambda: cmd_diff(g, rest), "sheet": lambda: cmd_sheet(g, rest), "gear": lambda: cmd_sheet(g, rest),
         "board": lambda: (print(board(g)), print(orders_needed(g)) if g.get("fight") else show_next(g)), "log": lambda: print("\n".join(g.get("tail", [])[-(int(rest[0]) if rest else 12):]))}
    if v not in T: die("Unknown command '%s'. delve.py help" % v)
    T[v]()
    if img and v == "do" and g.get("fight"): print("image: " + draw_fight(load()))

if __name__ == "__main__":
    main()
