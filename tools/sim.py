"""Balance check: a simple bot plays N expeditions per difficulty in a throwaway copy and reports how far it gets.
   python3 tools/sim.py [N] [class,class[,class,class]] [doors|maze]"""
import sys, os, io, json, random, contextlib, importlib.util, shutil, collections, time
os.environ["DELVE_NOGUARD"] = "1"; os.environ["DELVE_NOSEND"] = "1"
import tempfile
D = tempfile.mkdtemp(); shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "delve.py"), D + "/delve.py")
spec = importlib.util.spec_from_file_location("delve", D + "/delve.py"); dv = importlib.util.module_from_spec(spec); spec.loader.exec_module(dv)
ERR = collections.Counter()
def run(*a):
    buf = io.StringIO(); sys.argv = ["delve.py"] + [str(x) for x in a]
    try:
        with contextlib.redirect_stdout(buf): dv.main()
    except SystemExit: pass
    except Exception as e:
        ERR["%s: %s | %s" % (type(e).__name__, str(e)[:60], " ".join(map(str, a))[:40])] += 1
    return buf.getvalue()
def G(): return json.load(open(dv.SAVE))
SAFE = ["water", "damp", "moss", "quiet", "warm", "no tracks", "glint", "lock", "heavier", "humming", "lantern", "coins", "candle", "wax", "nothing special", "ordinary", "dust"]
def order(g, c):
    f = g["fight"]; groups = [gr for gr in f["groups"] if any(m["hp"] > 0 for m in gr["mons"])]
    known = dv.known(c); frac = lambda x: x["hp"] / x["maxhp"]
    low = sorted([x for x in g["party"] if x["hp"] > 0 or "down" in x["st"]], key=frac)
    idx = lambda gr: groups.index(gr) + 1
    weakest = min(groups, key=lambda gr: sum(m["hp"] for m in gr["mons"] if m["hp"] > 0))
    if "heal" in known and c["mp"] >= 1 and low and frac(low[0]) < .45: return ["cast", "heal", low[0]["name"]]
    if g["potions"] > 0 and low and frac(low[0]) < .30: return ["potion", low[0]["name"]]
    if c["cls"] == "mage":
        awake = [gr for gr in groups if "undead" not in dv.MON[gr["name"]][8] and sum(1 for m in gr["mons"] if m["hp"] > 0 and "asleep" not in m["st"]) >= 2]
        big = max(groups, key=lambda gr: sum(1 for m in gr["mons"] if m["hp"] > 0))
        if "frost" in known and c["mp"] >= 4: return ["cast", "frost", idx(big)]
        if "flame" in known and c["mp"] >= 3 and sum(1 for m in big["mons"] if m["hp"] > 0) >= 2: return ["cast", "flame", idx(big)]
        if awake and c["mp"] >= 2: return ["cast", "sleep", idx(awake[0])]
        if c["mp"] >= 1: return ["cast", "spark", idx(weakest)]
    if "banish" in known and c["mp"] >= 2:
        ud = [gr for gr in groups if "undead" in dv.MON[gr["name"]][8]]
        if ud: return ["cast", "banish", idx(ud[0])]
    if "ward" in known and c["mp"] >= 3 and not f.get("ward") and (f["boss"] or len(groups) > 1): return ["cast", "ward"]
    sleepers = [gr for gr in groups if any(m["hp"] > 0 and ("asleep" in m["st"] or "chilled" in m["st"]) for m in gr["mons"])]
    return ["attack", idx(sleepers[0] if sleepers else weakest)]
def expedition(diff, party, mode="doors", maxfloor=4, seed=None):
    if seed is not None: random.seed(seed)
    run("new", "--mode", mode, "--diff", diff, "--force")
    for i, cls in enumerate(party): run("hire", "H%d" % i, cls, "P%d" % i)
    g = G(); g["pics"] = False; json.dump(g, open(dv.SAVE, "w")); run("go")
    cleared = 0; steps = 0
    while steps < 4000:
        steps += 1; g = G()
        if g.get("over") or g["floor"] == 0: break
        for c in g["party"]:
            pk = dv.pending_picks(c)
            if pk: run("learn", c["name"], dv.TALENTS[c["cls"]][pk[0]][random.randint(0, 1)][0])
        if g.get("fight"):
            acted = False
            for c in g["party"]:
                if dv.can_act(c) and c["name"] not in g["fight"]["orders"]:
                    run("do", c["name"], *order(g, c)); acted = True; break
            if not acted: run("do", g["party"][0]["name"], "defend") if dv.can_act(g["party"][0]) else run("run")
            continue
        if g.get("loot"):
            it = g["loot"]["item"]; slot = dv.item_parse(it)[0]; took = False
            for c in g["party"]:
                if c["name"] in g["loot"]["need"] + g["loot"]["pass"]: continue
                better = not dv.can_use(c, it) and (slot == "charm" and not c.get("charm") or slot != "charm" and dv.item_value(it) > dv.item_value(dv.worn(c, slot)))
                run("loot", "need" if better else "pass", c["name"]); took = True; break
            if took: continue
        if g.get("shrine"): run("shrine", "leave"); continue
        if g.get("chest"): run("chest", "open"); continue
        hurt = [c for c in g["party"] if c["hp"] < c["maxhp"] * .5]
        if hurt and g["potions"] > 0 and (hurt[0]["hp"] < hurt[0]["maxhp"] * .35):
            run("use", hurt[0]["name"], "potion", hurt[0]["name"]); continue
        healer = [c for c in g["party"] if "heal" in dv.known(c) and c["mp"] >= 2 and c["hp"] > 0]
        if hurt and healer: run("cast", healer[0]["name"], "heal", hurt[0]["name"]); continue
        if g["mode"] == "doors":
            if g.get("stairs_open"):
                cleared = g["floor"]
                if cleared >= maxfloor: break
                run("stairs"); continue
            if g["step"] >= dv.DOOR_STEPS:
                if hurt and g.get("camped") != "%d:%d" % (g["floor"], g["step"]): run("camp")
                else: run("pick", 1)
                continue
            ds = g.get("doorset") or []
            if not ds: run("doors"); continue
            pick = next((i for i, dd in enumerate(ds) if any(s in dd["hint"] for s in SAFE)), 0) if hurt else random.randrange(len(ds))
            run("pick", pick + 1)
        else:
            f = g["floors"][str(g["floor"])]; here = f["feat"].get("%d,%d" % tuple(g["pos"]))
            if here == "down": 
                cleared = g["floor"]
                if cleared >= maxfloor: break
                run("stairs"); continue
            dn = [k for k, v in f["feat"].items() if v == "down" and k in f["seen"]]
            if dn: x, y = map(int, dn[0].split(",")); run("goto", dv.cell_name(x, y)); continue
            fr = dv.frontier(g)
            if not fr: break
            run("goto", random.choice(fr[:3])[0])
    g = G()
    return cleared, bool(g.get("over") or g.get("lost")), max(c["lvl"] for c in g["party"]), steps
if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    parties = [("paladin", "priest"), ("fighter", "mage")] if len(sys.argv) < 3 else [tuple(sys.argv[2].split(","))]
    mode = sys.argv[3] if len(sys.argv) > 3 else "doors"
    t0 = time.time()
    for party in parties:
        for diff in dv.DIFF_ORDER:
            res = [expedition(diff, party, mode) for _ in range(N)]
            c = collections.Counter(r[0] for r in res)
            print("%-16s %-9s cleared floor>=1: %3d%%  >=2: %3d%%  >=3: %3d%%  all4: %3d%% | fell: %3d%% | avg lvl %.1f | steps %d" % ("+".join(party), diff,
                  100 * sum(1 for r in res if r[0] >= 1) / N, 100 * sum(1 for r in res if r[0] >= 2) / N, 100 * sum(1 for r in res if r[0] >= 3) / N, 100 * c[4] / N,
                  100 * sum(1 for r in res if r[1]) / N, sum(r[2] for r in res) / N, sum(r[3] for r in res) / N))
    print("time %.0fs" % (time.time() - t0)); print("ERRORS:", dict(ERR) if ERR else "none")
    shutil.rmtree(D, ignore_errors=True)
