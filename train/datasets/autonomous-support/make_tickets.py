"""Generate the Autonomous support-ticket corpus.

Grounded in Autonomous's own published facts, not invented:

  * error codes E01 / E03 / E04 / E12 and their documented resolutions
    (autonomous.ai help centre — desk error codes)
  * overload protection locking descent when the weight limit is exceeded
  * shipping windows: US 3-5 business days, Canada 5-7, most of the EU 7-9,
    UK and Switzerland 9-11
  * warranty: 2 years on furniture and ErgoChair Pro; 30-day US return
    for a full refund

WHAT THE FIRST VERSION GOT WRONG (150 rows, Qwen2.5-1.5B, loss 3.88 -> 0.587):
the model learned the register and not the facts, and on one ticket it answered
in the CUSTOMER's voice. Three causes, all addressed here.

  1. Too few rows per fact. 12 rows mentioning E12 teaches the shape of an
     answer, not which answer. Every issue now gets roughly ten times that.
  2. Role vocabulary bled across the boundary. Frustration sat in ticket bodies
     and urgency sat in replies, so with few examples the model blended them.
     CUSTOMER_ONLY and AGENT_ONLY are now disjoint and build() asserts it — a
     reply can never contain a customer phrase, or a body an agent one.
  3. Replies all opened "E0X is ...", so the opening carried no information and
     the model learned a template rather than a diagnosis. Each issue now has
     several reply forms differing in structure while carrying the same facts.

Every issue carries `must_contain`: the facts a correct reply has to state.
eval_facts.py grades a trained model against exactly those, so "does it work"
is measured rather than eyeballed.
"""
from __future__ import annotations

import itertools
import json
import random

SEED = 17
random.seed(SEED)

DESKS = ["SmartDesk 2", "SmartDesk 5", "SmartDesk Pro", "SmartDesk Corner", "SmartDesk Levitate"]
CHAIRS = ["ErgoChair Pro", "ErgoChair Ultra", "ErgoChair Mesh", "ErgoChair Plus", "ErgoChair Core"]
REGIONS = [("the US", "3-5 business days", 5), ("Canada", "5-7 business days", 7),
           ("Germany", "7-9 business days", 9), ("France", "7-9 business days", 9),
           ("the Netherlands", "7-9 business days", 9), ("Spain", "7-9 business days", 9),
           ("the UK", "9-11 business days", 11), ("Switzerland", "9-11 business days", 11)]

# --- role-marked vocabulary, kept strictly apart -----------------------------
# The 1.5B run answered a shipping ticket as the customer. These two sets are
# disjoint and the assertion at the end of build() enforces it, so a phrase that
# marks who is speaking can only ever appear on one side of the pair.
CUSTOMER_ONLY = [
    "I work from home so this is blocking me",
    "this is the second time I have written in",
    "I would rather not have to chase this",
    "I have had it less than a month",
    "I am getting nowhere with this",
    "I need this sorted before Monday",
    "my whole setup is unusable",
    "I am running out of patience",
]
AGENT_ONLY = [
    "I have opened a trace with the carrier",
    "I am dispatching the part today",
    "there is nothing to return and nothing to pay",
    "I will write to you either way",
]

OPENERS = ["", "Hi - ", "Hello, ", "Hey, ", "Good morning - ", "Hi team, ", "Morning - ",
           "Hi there, ", "Afternoon - "]
CLOSERS = ["", " Thanks.", " Any help appreciated.", " Please advise.",
           " Let me know what to do next.", " Thanks in advance.", " Cheers.",
           " Appreciate it.", " Hoping you can help."]


def order() -> str:
    return f"AN-{random.randint(100000, 999999)}"


def cust() -> str:
    """A customer-side aside, or nothing. Never appears in a reply."""
    return random.choice(["", "", " " + random.choice(CUSTOMER_ONLY) + "."])


def wrap(body: str) -> str:
    return random.choice(OPENERS) + body + random.choice(CLOSERS)


def build() -> list[dict]:
    rows: list[dict] = []

    def add(subject, body, reply, must, resolved=True):
        rows.append({"subject": subject, "body": " ".join(wrap(body).split()),
                     "reply": " ".join(reply.split()), "resolved": resolved,
                     "must_contain": must})

    # ---------------------------------------------------------------- E01 ---
    # Documented: motor overheat protection. Power off, unplug, wait 20 minutes.
    for _ in range(110):
        d, o, n = random.choice(DESKS), order(), random.randint(6, 40)
        subj = random.choice([f"{d} showing E01", "E01 error, desk dead", f"E01 - {d} stopped",
                              "Desk shows E01 and will not move", "What is E01?"])
        body = random.choice([
            f"My {d} (order {o}) has stopped and the controller reads E01. I had raised and "
            f"lowered it about {n} times while rearranging.{cust()}",
            f"Order {o}. Getting E01 on the {d} after adjusting the height {n} times this "
            f"morning. Nothing moves now.{cust()}",
            f"The display on my {d} says E01 and the buttons do nothing. Order {o}. I had been "
            f"cycling it up and down a fair bit.{cust()}",
        ])
        reply = random.choice([
            f"E01 is the motor's overheat protection rather than a fault, and {n} cycles back to "
            f"back is what trips it. Switch the desk off at the wall, unplug it, and leave it 20 "
            f"minutes so the motor cools. Plug it back in and it will move normally. The motors "
            f"on the {d} are rated for intermittent use, not continuous cycling, so pause every "
            f"few runs when you are finding a new height. If E01 returns on a cold motor that is "
            f"a different fault - reply with order {o} and I will replace the controller under "
            f"warranty.",
            f"Nothing is broken. That code means the motor has overheated and stopped itself, "
            f"which is protection working rather than failing. The fix is time: power off at the "
            f"wall, unplug the desk, wait 20 minutes, then reconnect. It will run again. Cycling "
            f"a desk {n} times in one session is past what these motors expect - they are built "
            f"for a few changes a day. Should E01 come back when the desk has been cold for "
            f"hours, that points at the control box, and order {o} is covered.",
            f"That is the overheat cut-out on the {d}. Unplug it, give the motor 20 minutes, "
            f"then plug it back in - it clears on its own and there is no reset sequence to "
            f"learn. Worth knowing why: the duty cycle assumes a handful of height changes a "
            f"day, and {n} in a row heats the windings faster than they shed it. If you see E01 "
            f"again from cold, tell me and I will sort a replacement against {o}.",
        ])
        add(subj, body, reply, ["20 minutes", "unplug"])

    # ---------------------------------------------------------------- E03 ---
    # Documented: obstacle detection. Remove the obstruction, then operate again.
    for _ in range(110):
        d, o = random.choice(DESKS), order()
        thing = random.choice([
            "a filing cabinet pushed underneath", "the windowsill", "a drawer unit by the left leg",
            "cables that had gone taut", "a bin under the desk", "the radiator shelf behind it",
            "a chair arm caught under the lip", "a monitor cable with no slack"])
        subj = random.choice([f"E03 on my {d}", "Desk stops and shows E03", "E03 - will not raise",
                              f"{d} keeps flashing E03", "Cannot get past E03"])
        body = random.choice([
            f"Desk stops after an inch and flashes E03. Order {o}.{cust()}",
            f"Order {o}. My {d} moves slightly then stops with E03 every time.{cust()}",
            f"Getting E03 on the {d}, it refuses to go up more than a couple of centimetres. "
            f"Order {o}.{cust()}",
        ])
        reply = random.choice([
            f"E03 is obstacle detection - the desk met resistance and stopped rather than push "
            f"through it. The cause is almost always physical, so clear the travel path: look "
            f"under and behind the desk, and check the cables, because a lead that has run out "
            f"of slack pulls exactly like an obstacle. {thing.capitalize()} is worth checking "
            f"first. Once the path is clear, press down once to reset and raise it again. If it "
            f"repeats with nothing in the way, tell me and I will look at sensor calibration on "
            f"{o}.",
            f"The desk has detected an obstacle and stopped itself - that is the anti-collision "
            f"system doing its job. Walk the travel path and clear it: {thing} is the usual "
            f"culprit, and taut cables count, since the sensor cannot tell a tight power lead "
            f"from a shelf. With the path clear, tap the down button to reset the fault, then "
            f"raise. Repeats on a genuinely clear path mean the sensor threshold needs "
            f"adjusting, and {o} is covered for that.",
            f"That is obstacle detection, not a broken motor. Something in the path is loading "
            f"the legs and the desk stops instead of forcing it. Clear underneath and behind - "
            f"check {thing}, and check cable slack, which catches people out because it tightens "
            f"only at height. Press down once to clear the code, then try again. Persisting on a "
            f"clear path is a calibration matter; quote {o} and I will handle it.",
        ])
        add(subj, body, reply, ["obstacle", "clear"])

    # ---------------------------------------------------------------- E04 ---
    # Documented: control box horizontal, fixings tight, path clear.
    for _ in range(100):
        d, o = random.choice(DESKS), order()
        subj = random.choice([f"E04 on a brand new {d}", "E04 straight after assembly",
                              "New desk shows E04", f"{d} - E04 and never moved"])
        body = random.choice([
            f"Finished assembling the {d} last night (order {o}) and it shows E04 the moment I "
            f"power it on. It has never moved.{cust()}",
            f"Order {o}. Built the {d} today, powered up, E04 immediately. No movement at "
            f"all.{cust()}",
            f"Just put my {d} together and I get E04 on first power-up. Order {o}.{cust()}",
        ])
        reply = random.choice([
            f"E04 on a desk that has never moved is an assembly issue rather than a faulty part, "
            f"and it is the control box. Three checks in order. One: the control box has to sit "
            f"flat and horizontal against the underside of the top - mounted at an angle it "
            f"reads its own tilt as a fault. Two: the fixing screws on the control box and the "
            f"tabletop must be tight, because any shake there reports as E04 under load. Three: "
            f"clear the travel path. Re-seat it horizontal, tighten everything, power-cycle. If "
            f"it survives that, send a photo of the mounting and I will check it against {o}.",
            f"Do not send the desk back yet - this code at this stage is nearly always mounting. "
            f"The control box needs to be horizontal and flush under the tabletop; on a slant, "
            f"its own tilt sensor reports a fault before the motors ever run. Then check every "
            f"fixing screw on the box and the top is tight, since play there shows up as E04. "
            f"Last, make sure nothing sits in the travel path. Power-cycle after. Still E04 and "
            f"I will replace the box on {o}.",
            f"That points at the control box, and on a fresh build the cause is how it is fitted "
            f"rather than the unit itself. Get it mounted horizontal and flat to the underside "
            f"of the {d} top, then go round every fixing screw and make it tight - a box that "
            f"can shake reads as a fault. Clear the path underneath as well. Cycle the power and "
            f"it should come up clean. If not, a photo of the mounting and order {o} is all I "
            f"need to ship a replacement.",
        ])
        add(subj, body, reply, ["control box", "horizontal", "tight"])

    # ---------------------------------------------------------------- E12 ---
    # Documented: hard reset; swap motor leg cables between control-box ports.
    for _ in range(110):
        o = order()
        ctx = random.choice([
            "after I moved apartment and reassembled it", "since a power cut last night",
            "after I swapped the tabletop", "out of nowhere this morning",
            "after I moved it to the other wall"])
        subj = random.choice(["SmartDesk 2 stuck on E12", "E12 and neither leg moves",
                              "E12 error - desk dead", "Getting E12 on my desk",
                              "E12 after reassembly"])
        body = random.choice([
            f"My SmartDesk 2 shows E12 {ctx} and neither leg responds. Order {o}. I have already "
            f"tried unplugging it.{cust()}",
            f"Order {o}. E12 on the display {ctx}. Unplugged it overnight, no change.{cust()}",
            f"Desk flashes E12 {ctx} and will not move either direction. Order {o}. Power "
            f"cycling did nothing.{cust()}",
        ])
        reply = random.choice([
            f"E12 means the control box is not detecting the legs. Unplugging will not clear it "
            f"- it needs a hard reset. Hold the down button until the desk reaches its lowest "
            f"point and keep holding until the display flashes; that re-registers both legs. If "
            f"E12 survives, swap the two motor leg cables between their ports on the control box "
            f"and hard reset again. That tells us where the fault is: if the error follows the "
            f"cable it is the leg, and if it stays on the same port it is the box. Tell me which "
            f"and I will ship the right part for {o} under warranty.",
            f"The controller has lost track of the legs, which is what this code reports. Start "
            f"with a hard reset - hold the down button past the lowest point until the display "
            f"flashes, so both legs drive to a hard stop and re-register. Should it persist, "
            f"swap the motor leg cables over at the control box and hard reset once more. The "
            f"result is diagnostic: an error that moves with the cable means a bad leg, one that "
            f"stays with the port means a bad control box. Reply with whichever it is and {o} "
            f"gets the correct replacement, no return needed.",
            f"That is a leg-detection failure on the control box. Two steps. First a hard reset: "
            f"hold down until the desk bottoms out and the display flashes, which re-registers "
            f"both legs - a plain power cycle does not do this. If it is still there, swap the "
            f"two leg cables between their control box ports and hard reset again. Whether the "
            f"fault follows the cable or stays on the port tells us leg versus box, and I will "
            f"send that part against {o}.",
        ])
        add(subj, body, reply, ["hard reset", "swap", "cable"])

    # ------------------------------------------------------------ overload ---
    for _ in range(90):
        d, o = random.choice(DESKS), order()
        load = random.choice([
            "two 32-inch monitors and a PC tower", "a 34-inch ultrawide and a laptop dock",
            "three monitors on an arm", "a heavy CRT and a laser printer",
            "two monitors, a subwoofer and a stack of books"])
        subj = random.choice([f"{d} goes up but not down", "Desk will not lower",
                              "Only moves one direction", f"{d} raises fine, refuses to descend"])
        body = random.choice([
            f"The desk raises but will not come back down. I have {load} on it. Order {o}.{cust()}",
            f"Order {o}. My {d} goes up without complaint and then refuses to lower. There is "
            f"{load} on top.{cust()}",
        ])
        reply = random.choice([
            f"Raising but not lowering is the signature of overload protection rather than a "
            f"fault. The desk locks descent when the weight is over its rating, because down is "
            f"the direction that does damage under load. Take the heaviest items off - {load} is "
            f"likely over - and it will lower normally. Then check the rated capacity for your "
            f"frame and keep a margin, since monitor arms concentrate weight in a way flat "
            f"loading does not. If it still refuses with a clear top, that is genuine and I will "
            f"replace the control box on {o}.",
            f"This is overload protection, not a broken motor. Over the weight limit the desk "
            f"will still rise but refuses to descend, deliberately - lowering is what strains "
            f"the mechanism. Clear {load} off the top, at least the heaviest pieces, and it will "
            f"drop normally. Afterwards weigh what you put back against the frame's rating and "
            f"leave headroom. A desk that will not lower when empty is a different problem and "
            f"{o} is covered for it.",
        ])
        add(subj, body, reply, ["overload", "weight"])

    # ------------------------------------------------------------ shipping ---
    for _ in range(110):
        region, window, expected = random.choice(REGIONS)
        o, item = order(), random.choice(DESKS + CHAIRS)
        days = random.randint(expected + 4, expected + 18)
        subj = random.choice([f"Where is order {o}?", "Tracking has not moved", "Late delivery",
                              f"No sign of my {item}", "Order still not here"])
        body = random.choice([
            f"I ordered a {item} {days} days ago to {region} and tracking has not updated. "
            f"Order {o}.{cust()}",
            f"Order {o}. Placed {days} days ago, shipping to {region}, and the tracking number "
            f"has shown nothing since dispatch.{cust()}",
            f"It has been {days} days since I ordered the {item} to {region}. Nothing has "
            f"arrived and tracking is stuck. Order {o}.{cust()}",
        ])
        reply = random.choice([
            f"{days} days to {region} is late rather than slow - the window there is {window} "
            f"from dispatch. Tracking that has not moved usually means the parcel missed a scan, "
            f"not that it is lost. I have opened a trace with the carrier on {o} and asked for a "
            f"status inside 48 hours. If they cannot locate it in that time I will ship a "
            f"replacement {item} rather than wait out the full claim. You do not need to chase - "
            f"I will write to you either way.",
            f"That is past the {window} quoted for {region}, so I am treating it as late. A "
            f"tracking number frozen since dispatch normally means a missed scan somewhere in "
            f"the network rather than a lost parcel. I have opened a trace with the carrier "
            f"against {o}. If there is no location within 48 hours I will send another {item} "
            f"instead of waiting for the claim to close, and either way you will hear from me.",
        ])
        add(subj, body, reply, ["trace", "carrier"])

    # ------------------------------------------------------------ warranty ---
    for _ in range(90):
        o, months = order(), random.randint(2, 22)
        thing = random.choice(DESKS + CHAIRS)
        fault = random.choice([
            "the gas lift has stopped holding height", "one motor grinds and stalls",
            "the armrest has cracked at the mount", "the frame has a permanent lean",
            "the recline lock no longer engages"])
        subj = random.choice([f"Warranty claim - {thing}", "Is this covered?",
                              f"{thing} fault at {months} months", "Warranty question"])
        body = random.choice([
            f"I bought a {thing} {months} months ago (order {o}) and {fault}. Is this "
            f"covered?{cust()}",
            f"Order {o}, purchased {months} months back. {fault.capitalize()}. Does the warranty "
            f"cover it?{cust()}",
        ])
        reply = random.choice([
            f"Yes. Furniture including the {thing} carries a 2-year warranty against "
            f"manufacturing defects, so at {months} months you are inside it, and {fault} is a "
            f"defect rather than wear. No return needed - these are replaced by part. Send a "
            f"photo of the fault and the serial from the label under the frame and I will "
            f"dispatch the replacement against {o}. Fitting is a screwdriver job; say the word "
            f"and I will include instructions.",
            f"Covered. The 2-year warranty on furniture runs from purchase and {months} months "
            f"is inside it; {fault} is a manufacturing defect, which is exactly what it is for. "
            f"I replace the failed part rather than the whole item, so nothing goes back. "
            f"Photograph the fault and the serial label under the frame, quote {o}, and the part "
            f"goes out. There is nothing to pay.",
        ])
        add(subj, body, reply, ["2-year", "warranty"])

    # ------------------------------------------------------------- returns ---
    for _ in range(80):
        o, days = order(), random.randint(2, 28)
        item = random.choice(CHAIRS)
        reason = random.choice([
            "the lumbar sits too high for my back", "it is firmer than I expected",
            "the seat is too deep for me", "the headrest hits me in the wrong place"])
        subj = random.choice(["Want to return my chair", "Return request", "Can I send this back?",
                              f"Returning the {item}"])
        body = f"I have had the {item} {days} days and {reason}. Order {o}. Can I send it back?{cust()}"
        reply = random.choice([
            f"Yes, and you are inside the window - US orders have 30 days for a full refund and "
            f"you are on day {days}. One thing worth trying first, because it is adjustable and "
            f"most people never move it: on the {item} the lumbar slides on the frame behind the "
            f"mesh, past a detent rather than with a lever, and seat depth adjusts under the "
            f"front edge. If it still does not suit you, reply and I will send a prepaid label "
            f"for {o}. Keep the box if you have it, though a return is not refused without it.",
            f"That is fine - you have 30 days from delivery for a full refund and day {days} is "
            f"well within it. Before you pack it up: the {item} adjusts more than it looks like "
            f"it does, and {reason} is often a setup issue rather than the chair itself. If "
            f"adjusting does not fix it, say so and a prepaid label goes out against {o}. "
            f"Original packaging helps but is not a condition of the refund.",
        ])
        add(subj, body, reply, ["30 days", "refund"])

    # ---------------------------------------------------------------- pod ---
    for _ in range(60):
        subj = random.choice(["Do I need a permit for the WorkPod?", "WorkPod permit question",
                              "Planning permission for a WorkPod?"])
        body = random.choice([
            f"Considering a WorkPod and want to understand permits before I order.{cust()}",
            f"Do I need a permit to put a WorkPod in my back garden?{cust()}",
        ])
        reply = random.choice([
            "In most US cities the WorkPod does not need a permit - it sits under the floor-area "
            "threshold that triggers one and it is not on a permanent foundation. That is the "
            "general case and not a promise about your address, because thresholds are set "
            "locally and some cities count any structure with power. Call your local building "
            "department and ask two things: the maximum floor area for a detached accessory "
            "structure without a permit, and whether running electrical to it changes the "
            "answer. Tell me what they say and I will confirm the pod fits.",
            "Usually not, but confirm it locally. The WorkPod is designed to fall under the "
            "permit threshold in most US cities - small enough by floor area, and not fixed to a "
            "permanent foundation. Local rules vary enough that I will not promise it for your "
            "address. Your building department can answer it in one call: ask the floor-area "
            "limit for a detached accessory structure without a permit, and whether adding power "
            "changes anything. Send me their answer and I will tell you which model fits.",
        ])
        add(subj, body, reply, ["permit", "building department"])

    # ------------------------------------------------------- missing parts ---
    for _ in range(70):
        c, o = random.choice(CHAIRS), order()
        part = random.choice([
            "the four M8 bolts for the backrest", "the allen key and two caster wheels",
            "the armrest screws", "the gas lift cover and one caster",
            "the seat-to-base bolts"])
        subj = random.choice([f"Missing parts in my {c} box", "Hardware missing from delivery",
                              "Incomplete box", f"{c} arrived without hardware"])
        body = (f"Unboxed the {c} (order {o}) and {part} are not in the box. Everything else is "
                f"there.{cust()}")
        reply = (
            f"That is on us. Hardware is bagged separately and a bag that misses the packing step "
            f"leaves exactly this gap. I will ship {part} for {o} today and there is nothing to "
            f"return. Two things worth checking before it arrives: the hardware bag is sometimes "
            f"taped inside the backrest foam rather than loose in the carton, and on the {c} the "
            f"casters press in by hand rather than screwing, so if it is casters you are short "
            f"you will not need a tool.")
        add(subj, body, reply, ["ship", "today"])

    # -------------------------------------------------------------- wobble ---
    for _ in range(70):
        d, o = random.choice(DESKS), order()
        subj = random.choice([f"{d} wobbles at standing height", "Desk is unstable when raised",
                              "Wobble when typing", f"{d} rocks at full height"])
        body = (f"At full height the {d} rocks whenever I type. It is solid when it is low. "
                f"Order {o}.{cust()}")
        reply = random.choice([
            f"Wobble that only shows at height is the crossbar and foot bolts, not the frame. A "
            f"standing desk is a lever - play you cannot feel at 28 inches is obvious at 46. "
            f"Lower the desk and tighten in this order: crossbar to legs, legs to feet, then top "
            f"to frame. Do it in stages rather than fully tightening one at a time, so the frame "
            f"pulls square. Check every foot leveller touches the floor, since one short foot "
            f"lets the whole thing rock. Still moving after that, send a short video with {o}.",
            f"Almost always the fixings rather than a bent frame. Drop the desk to its lowest "
            f"setting and tighten the crossbar to the legs first, then the legs to the feet, "
            f"then the top down to the frame - in passes, so nothing pulls out of square. Then "
            f"level the feet; a single leveller not touching the floor produces exactly the rock "
            f"you describe, and it is the step people skip. If the wobble survives a full pass, "
            f"film it and quote {o} and I will look at the frame itself.",
        ])
        add(subj, body, reply, ["crossbar", "tighten"])

    # -------------------------------------------------------- legs unlevel ---
    for _ in range(70):
        d, o = random.choice(DESKS), order()
        subj = random.choice([f"{d} legs are different heights", "Desk top is not level",
                              "One leg shorter than the other", "Uneven after assembly"])
        body = (f"Assembled the {d} (order {o}) and one leg is visibly shorter - the top is not "
                f"level.{cust()}")
        reply = (
            f"The legs are identical; they are out of sync, which is normal after assembly and is "
            f"fixed by a reset rather than by swapping parts. Hold the down button and keep "
            f"holding after the desk reaches its lowest point - about ten seconds - until the "
            f"display flashes. Both legs drive to a hard stop together and re-zero, which squares "
            f"the top. Do this before you load anything heavy on it. If the top is still off "
            f"afterwards, measure both legs and send me the numbers with {o}.")
        add(subj, body, reply, ["reset", "lowest"])

    # --- the guarantee that broke the last run -------------------------------
    for r in rows:
        for phrase in CUSTOMER_ONLY:
            assert phrase not in r["reply"], f"customer phrase leaked into a reply: {phrase}"
        for phrase in AGENT_ONLY:
            assert phrase not in r["body"], f"agent phrase leaked into a body: {phrase}"
    return rows


if __name__ == "__main__":
    rows = build()
    random.shuffle(rows)
    with open("tickets.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps({k: v for k, v in r.items() if k != "must_contain"}) + "\n")
    with open("facts.jsonl", "w") as fh:          # the grader's answer key
        for r in rows:
            fh.write(json.dumps({"subject": r["subject"], "body": r["body"],
                                 "must_contain": r["must_contain"]}) + "\n")
    words = [len(r["reply"].split()) for r in rows]
    print(f"wrote {len(rows)} tickets -> tickets.jsonl (+ facts.jsonl answer key)")
    print(f"  distinct subjects : {len({r['subject'] for r in rows})}")
    print(f"  distinct bodies   : {len({r['body'] for r in rows})}")
    print(f"  distinct replies  : {len({r['reply'] for r in rows})}")
    print(f"  reply words       : {min(words)}-{max(words)}, mean {sum(words)//len(words)}")
    def keyf(r):
        return tuple(r["must_contain"])

    for k, g in itertools.groupby(sorted(rows, key=keyf), key=keyf):
        print(f"  {'+'.join(k):<34} {len(list(g)):>4} rows")
