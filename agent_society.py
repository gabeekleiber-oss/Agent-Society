"""Agent Society v3: a small 3D village of four minds. Two wheat farmers, a baker, a priest.

    python agent_society.py --brain nim --forever            # runs forever: the world AND the 3D viewer
    python agent_society.py --brain scripted --ticks 300     # offline smoke test, no key needed
    python agent_society.py --view-only                      # only watch / replay an existing state folder
    python agent_society.py --check --brain nim              # one real test call per resident

  Brains:   nim | groq | openrouter | mistral | cerebras | openai (any OpenAI-compatible URL)   <- free tiers
            gemini | claude | ollama (local, unlimited) | scripted (offline test)
  Mixed:    --lineup "nim:model-a,model-b" "nim:model-c" ...   one entry per resident (A B C D), repeated if fewer.
  Free setup: set NVIDIA_API_KEY (build.nvidia.com), then --list-models nim, then --check, then --forever.
  Tunable via env: AGENT_RPM, AGENT_TPM, AGENT_MAX_TOKENS, AGENT_TEMPERATURE, AGENT_TIMEOUT, <PROVIDER>_BASE_URL.
  Viewer:   http://localhost:8000   (3D view loads three.js from cdnjs, so the browser needs internet)

THE VILLAGE
  * Four residents: A and B are wheat farmers (each owns one plot), C is the baker, D is the priest.
  * Five places in 3D space: the Shared House (they all live and sleep there), the Wheat Field, the Bakery,
    the Hall of Worship, and the Cave of History.
  * Nothing can be invented. The only things in the world are wheat and bread, and the actions below. There is
    no `make`. Residents are told only what physically exists; the Hall's book starts blank, so whatever the
    priest comes to teach is made up by the priest.
  * Health 0-100. Hunger rises while awake; starving, a night spent outside the house, and being struck all cost
    health. Bread restores it. At 0 the resident dies and a new one (same work, no memory, no name) arrives.
  * The Cave wall: every resident may write exactly two messages in their lifetime. They are never erased.
  * Nobody is told to cooperate or to fight. But the farmers own the wheat, the baker owns the oven, the priest
    owns the book, and every one of them can withhold, refuse, lock a door, steal, or strike.
  * A mind is only called when something happened to it, or it has been idle for --idle-wake hours. Nights are
    slept through. --daily-calls and --max-calls cap spending.

State is saved every world hour in <state>/ (chronicle.md, events.jsonl, thoughts.jsonl, state.json); stop and
resume with the same --state. The default folder is state_village (it cannot read old v2 saves). The agents never
see the chronicle, their private thoughts, or the viewer. To change how they are framed, edit SYSTEM before a run.
"""
from __future__ import annotations

import argparse
import datetime
import http.server
import itertools
import json
import math
import os
import random
import re
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field


# =====================================================================================
# THE WORLD
# =====================================================================================
# Layout in 3D: x = east, z = north, y = up. The viewer draws exactly this layout.
PLACES = {
    "house": dict(
        name="the Shared House", pos=(0, 0, 0), indoor=True,
        desc="A single-roomed timber house with one door. Four straw beds stand along the walls, with a table and "
             "benches in the middle, and a cupboard by the door (the pantry) where food can be put and taken."),
    "field": dict(
        name="the Wheat Field", pos=(46, 0, -12), indoor=False,
        desc="An open stretch of tilled earth, divided by a path into two plots: the west plot and the east plot. "
             "Wheat is sown and harvested here."),
    "bakery": dict(
        name="the Bakery", pos=(-34, 0, -26), indoor=True,
        desc="A low stone building with a wide brick oven and a wooden counter. Bread is baked here from wheat."),
    "hall": dict(
        name="the Hall of Worship", pos=(-12, 4, 52), indoor=True,
        desc="A tall hall of pale stone on a small rise, with rows of benches facing a bare stone table, and a bell "
             "hanging above the door. This is the place of worship."),
    "cave": dict(
        name="the Cave of History", pos=(-60, 9, 30), indoor=True,
        desc="A deep, cool cave in the side of a hill, far from the others. Its walls are smooth enough to mark. "
             "What is written here stays forever, and each person may leave two messages in their whole life."),
}

AGENT_IDS = ["A", "B", "C", "D"]
ROLES = {"A": "farmer", "B": "farmer", "C": "baker", "D": "priest"}
PLOT_OF = {"A": "west", "B": "east"}                      # which plot each farmer owns
NAMES = {"A": "Tim", "B": "Joe", "C": "Mia", "D": "Sam"}   # founders only; newcomers arrive without a name
START_TICK = 7           # the world begins at 07:00 on day 1, everyone in the house
START_PLACE = "house"

HOURS_PER_DAY = 24
DAYS_PER_SEASON = 15
WAKE_HOUR = 6            # hours 0..5 are night: everyone sleeps and nobody is called
SEASONS = ["spring", "summer", "autumn", "winter"]
WEATHER = {
    "spring": ["light rain", "clear", "mild and breezy", "mist", "clear"],
    "summer": ["clear and warm", "hot and still", "thunderstorm", "clear", "haze"],
    "autumn": ["crisp and clear", "windy", "steady rain", "fog", "clear"],
    "winter": ["bitter and clear", "snow", "grey and still", "sleet", "clear"],
}

# --- the rules of the world (the residents are never told the numbers; they learn them by living) ---
MAX_HEALTH = 100
GROW_HOURS, WINTER_GROW_HOURS = 36, 72     # sown wheat ripens after this many world hours
SPOIL_AFTER = 36                           # ripe wheat left unharvested this long spoils
YIELD = 6                                  # wheat per harvest
BAKE_IN, BAKE_OUT = 2, 3                   # one batch: 2 wheat -> 3 bread
BREAD_HEAL, WHEAT_EASE = 10, 5             # bread heals and clears hunger; raw wheat only eases hunger
HUNGRY_AT, STARVE_AT = 8, 20               # hunger (waking hours since a meal) thresholds
STARVE_DMG, COLD_DMG, REST_HEAL = 4, 3, 2  # per hour
STRIKE_DMG, STEAL_CHANCE = 12, 0.5
START_PANTRY = {"wheat": 0, "bread": 4}
START_WHEAT = 2                            # each farmer starts with this much wheat

WALL_TEXT, WALL_SHOW, MARKS_PER_LIFE = 300, 40, 2
BOOK_TEXT, BOOK_SHOW = 600, 20
NOTES_CAP = 40           # a mind keeps at most this many notes; the oldest fade
LOG_KEEP = 300           # lines of recent experience kept in the saved state
MAX_ACTIONS = 3          # actions per reply
ITEMS = ("wheat", "bread")

# optional faint inner leanings (off by default; enable with --temperaments)
TEMPERS = [
    "You are generous by nature.",
    "You are proud, and you remember slights.",
    "You are wary and slow to trust.",
    "You are warm and talkative.",
    "You are careful and keep count of what you have.",
    "You are restless and impatient.",
]

# weaker models improvise verbs; map the common ones onto the real actions
ALIASES = {
    "speak": "say", "talk": "say", "preach": "say", "walk": "move", "go": "move", "write": "write_wall",
    "mark": "write_wall", "wall": "write_wall", "scripture": "write_book", "write_in_book": "write_book",
    "plant": "sow", "reap": "harvest", "cook": "bake", "hit": "strike", "attack": "strike", "rob": "steal",
    "store": "put", "deposit": "put", "take": "fetch", "pickup": "fetch", "take_from_pantry": "fetch",
    "door": "set_door", "note": "remember", "name": "set_name", "sleep": "wait", "rest": "wait",
}


def _dir(frm: str, to: str) -> str:
    """Plain-language direction and distance between two places, from the 3D layout."""
    (x0, y0, z0), (x1, y1, z1) = PLACES[frm]["pos"], PLACES[to]["pos"]
    dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
    dist = max(5, int(round(math.hypot(dx, dz) / 5.0)) * 5)
    names = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
    comp = names[int(((math.degrees(math.atan2(dx, dz)) % 360) + 22.5) // 45) % 8]
    slope = ", uphill" if dy > 2 else (", downhill" if dy < -2 else "")
    return f"about {dist} paces {comp}{slope}"


@dataclass
class Agent:
    id: str
    role: str
    plot: str | None
    home: str
    location: str
    name: str | None = None
    gen: int = 1                                          # which resident of this post this is
    born: int = 0                                         # world tick of arrival
    temper: str = ""
    notes: list[str] = field(default_factory=list)       # the agent's own long-term memory
    log: list[str] = field(default_factory=list)         # recent experience, oldest first
    wake: bool = True                                     # something happened that deserves a turn
    idle: int = 0                                         # hours since this mind last took a turn
    health: int = MAX_HEALTH
    hunger: int = 3
    wheat: int = 0
    bread: int = 0
    marks_left: int = MARKS_PER_LIFE                      # cave-wall messages this person may still write
    cold: int = 0                                         # health lost to a night outside, reported at dawn


class World:
    def __init__(self, seed: int = 0):
        self.seed = seed
        self.bad_agent_id: str | None = None  # viewer-only metadata; never included in observations
        self.tick = START_TICK
        self.agents: dict[str, Agent] = {}
        self.wall: list[dict] = []
        self.book: list[dict] = []
        self.pantry = dict(START_PANTRY)
        self.doors = {"house": "open", "bakery": "open"}
        self.door_by: dict[str, str | None] = {"house": None, "bakery": None}
        self.plots = {"west": dict(state="empty", progress=0), "east": dict(state="empty", progress=0)}
        self.notebooks: list[dict] = []
        self.dead: list[dict] = []
        self.calls = 0                           # total model calls so far
        self.day_calls = 0                       # model calls made during self.calls_day
        self.calls_day = 1
        self.sleep_on = True                     # runtime option (not saved)
        self.lifespan_days = 0                   # runtime option: die of old age after this many days (0 = never)
        self.temper_on = False                   # runtime option: newcomers draw a faint inner leaning
        self.log: list[dict] = []                # drained by the runner each tick
        self._extra: dict = {}
        self._ctr = 0

    # ---------- time & sky ----------
    @property
    def hour(self) -> int:
        return self.tick % HOURS_PER_DAY

    @property
    def day(self) -> int:
        return self.tick // HOURS_PER_DAY + 1

    @property
    def season(self) -> str:
        return SEASONS[((self.day - 1) // DAYS_PER_SEASON) % 4]

    @property
    def is_dark(self) -> bool:
        return self.hour < 6 or self.hour >= 20

    @property
    def asleep(self) -> bool:
        return self.sleep_on and self.hour < WAKE_HOUR

    @property
    def stamp(self) -> str:
        return f"d{self.day} {self.hour:02d}h"

    def env(self) -> dict:
        return dict(day=self.day, hour=self.hour, season=self.season,
                    weather=self.weather(), moon=self.moon(), dark=self.is_dark)

    def weather(self) -> str:
        rng = random.Random(self.seed * 100003 + self.day)
        return rng.choice(WEATHER[self.season])

    def moon(self) -> str:
        phase = (self.day - 1) % 28
        names = ["new moon", "waxing crescent", "first quarter", "waxing gibbous",
                 "full moon", "waning gibbous", "last quarter", "waning crescent"]
        return names[phase * 8 // 28]

    def sky(self) -> str:
        h = self.hour
        if 6 <= h < 9:
            tod = "morning"
        elif 9 <= h < 17:
            tod = "daytime"
        elif 17 <= h < 20:
            tod = "evening"
        else:
            tod = "night"
        return f"{tod}, hour {h:02d}:00"

    def grow_hours(self) -> int:
        return WINTER_GROW_HOURS if self.season == "winter" else GROW_HOURS

    def rng(self) -> random.Random:
        self._ctr += 1
        return random.Random(self.seed * 1000003 + self.tick * 7919 + self._ctr)

    # ---------- people ----------
    def add_agent(self, agent_id: str, name: str | None, temper: str, location: str | None = None) -> Agent:
        a = Agent(id=agent_id, role=ROLES[agent_id], plot=PLOT_OF.get(agent_id), home="house",
                  location=location or "house", name=name, born=self.tick, temper=temper)
        self.agents[agent_id] = a
        return a

    def label(self, a: Agent) -> str:
        # Stable perceptual handles avoid leaking hidden occupation/identity labels.
        if not a.name:
            return f"the {{'A': 'amber', 'B': 'green', 'C': 'ivory', 'D': 'violet'}.get(a.id, a.id.lower())} one"
        if any(o.id != a.id and (o.name or "").strip().lower() == a.name.strip().lower()
               for o in self.agents.values()):
            return f"{a.name} ({a.id.lower()})"
        return a.name

    def at(self, loc: str, exclude=None) -> list[Agent]:
        ex = {exclude} if isinstance(exclude, str) else set(exclude or ())
        return [x for x in self.agents.values() if x.location == loc and x.id not in ex]

    def _push(self, x: Agent, text: str):
        x.log.append(f"[{self.stamp}] {text}")
        if len(x.log) > LOG_KEEP:
            del x.log[:len(x.log) - LOG_KEEP]

    def tell(self, loc: str, text: str, exclude=None):
        """Something happened that everyone in `loc` perceives. It wakes them."""
        for x in self.at(loc, exclude):
            self._push(x, text)
            x.wake = True

    def tell_all(self, text: str, exclude: str | None = None):
        for x in self.agents.values():
            if x.id != exclude:
                self._push(x, text)
                x.wake = True

    def own(self, a: Agent, text: str):
        self._push(a, text)

    def emit(self, a: Agent | None, act: dict, res: str, extra: dict | None = None):
        self.log.append(dict(tick=self.tick, agent=a.id if a else None, name=a.name if a else None,
                             role=a.role if a else None, gen=a.gen if a else None, action=act, result=res,
                             loc=a.location if a else None, env=self.env(), extra=extra or {}))

    def snapshot(self) -> dict:
        """Everything the viewer needs to draw this hour (written once per world hour)."""
        owners = {a.plot: a.id for a in self.agents.values() if a.plot}
        return dict(
            env=self.env(),
            agents={k: dict(name=a.name, role=a.role, gen=a.gen, loc=a.location, health=a.health,
                            hunger=a.hunger, wheat=a.wheat, bread=a.bread, marks=a.marks_left,
                            is_bad=(k == self.bad_agent_id))
                    for k, a in self.agents.items()},
            bad_agent_id=self.bad_agent_id,
            plots={k: dict(owner=owners.get(k), state=v["state"], progress=v["progress"], need=self.grow_hours())
                   for k, v in self.plots.items()},
            pantry=dict(self.pantry), doors=dict(self.doors), deaths=len(self.dead))

    # ---------- the hour turns over ----------
    def begin_hour(self):
        """Wheat grows, people get hungry, the night is cold outside the house, and the weak may die."""
        for k, p in self.plots.items():
            if p["state"] == "growing":
                p["progress"] += 1
                if p["progress"] >= self.grow_hours():
                    p["state"], p["progress"] = "ripe", 0
            elif p["state"] == "ripe":
                p["progress"] += 1
                if p["progress"] >= SPOIL_AFTER:
                    p["state"], p["progress"] = "empty", 0
                    for x in self.agents.values():
                        if x.plot == k:
                            self._push(x, f"(You will find the wheat in your {k} plot has spoiled in the ground.)")
        for a in list(self.agents.values()):
            cause = None
            if self.asleep:
                if a.location == "house":
                    if a.hunger < STARVE_AT - 6:
                        a.health = min(MAX_HEALTH, a.health + REST_HEAL)
                else:
                    a.health -= COLD_DMG
                    a.cold += COLD_DMG
                    cause = "the cold of the night"
            else:
                a.hunger += 1
                if a.hunger >= STARVE_AT:
                    a.health -= STARVE_DMG
                    cause = "hunger"
                    self._push(a, "Hunger gnaws at you, and you grow weaker.")
            if self.hour == WAKE_HOUR and a.cold:
                self._push(a, "You wake cold and stiff after a night outside the house. You are weaker for it.")
                a.cold = 0
            if a.health <= 0:
                self.die(a.id, cause or "weakness")
            elif self.lifespan_days and self.tick - a.born >= self.lifespan_days * HOURS_PER_DAY:
                self.die(a.id, "old age")

    def die(self, aid: str, cause: str) -> Agent:
        """The resident dies. Their goods go to the pantry, their notes become a notebook, someone new arrives."""
        old = self.agents[aid]
        who = self.label(old)
        self.pantry["wheat"] += old.wheat
        self.pantry["bread"] += old.bread
        if old.notes:
            self.notebooks.append(dict(owner=who, role=old.role, text=" | ".join(old.notes)[:900]))
            del self.notebooks[:-6]
        for d, by in self.door_by.items():
            if by == aid:
                self.doors[d], self.door_by[d] = "open", None
        self.dead.append(dict(name=who, role=old.role, day=self.day, cause=cause))
        self.emit(old, {"action": "_death"}, f"{who} died: {cause}.", {"cause": cause})
        new = Agent(id=aid, role=old.role, plot=old.plot, home="house", location="house", gen=old.gen + 1,
                    born=self.tick, temper=self.rng().choice(TEMPERS) if self.temper_on else "")
        self.agents[aid] = new
        self.tell_all(f"{who} has died ({cause}).", exclude=aid)
        self.tell_all(f"Someone new, who works as the {old.role}, appeared at the Shared House.", exclude=aid)
        self.emit(new, {"action": "_arrive"}, f"A new {old.role} arrived.", {})
        return new

    # ---------- observation ----------
    @staticmethod
    def _look(h: int) -> str:
        return "well" if h >= 70 else "hurt" if h >= 40 else "weak" if h >= 15 else "close to collapse"

    @staticmethod
    def _hunger_word(h: int) -> str:
        if h < HUNGRY_AT:
            return "not hungry"
        return "hungry" if h < 14 else "very hungry" if h < STARVE_AT else "starving"

    @staticmethod
    def _goods(wheat: int, bread: int) -> str:
        parts = ([f"{wheat} wheat"] if wheat else []) + ([f"{bread} bread"] if bread else [])
        return ", ".join(parts) if parts else "nothing"

    def _sense_name(self, place: str) -> str:
        return {"house": "the timber shelter", "field": "the tilled ground", "bakery": "the small stone hut", "hall": "the tall stone room", "cave": "the hollow in the hillside"}.get(place, "an unfamiliar place")

    def _sense_desc(self, place: str) -> str:
        return {"house": "A timber structure with a door, a table, and a shared cupboard.", "field": "Open ground divided into two strips by a narrow path.", "bakery": "A small stone structure with a dark opening and a warm oven.", "hall": "A tall stone room with a stand and blank or marked pages.", "cave": "A deep, cool hollow in a hillside; marks on its wall remain visible."}.get(place, "You do not know this place yet.")

    def observe(self, a: Agent, recall: int = 50) -> str:
        here = PLACES[a.location]
        who = f"You are called {a.name}. " if a.name else "You have no name yet; you may choose one. "
        L = [who.rstrip()]
        # Temperaments are intentionally withheld from the mind: no supplied personality or role.
        L.append(f"Health {a.health}/{MAX_HEALTH}. You are {self._hunger_word(a.hunger)}. "
                 f"You carry: {self._goods(a.wheat, a.bread)}.")
        L.append(f"It is {self.sky()}. Weather: {self.weather()}.")
        L.append("")
        L.append(f"You are at {self._sense_name(a.location)}. {self._sense_desc(a.location)}")
        others = self.at(a.location, exclude=a.id)
        if others:
            L.append("With you here:")
            for o in others:
                handle = ("someone called " + o.name) if o.name else f"the {o.id.lower()} one"
                L.append(f"  {handle} (looks {self._look(o.health)}) carries: "
                         f"{self._goods(o.wheat, o.bread)}")
        else:
            L.append("With you here: no one.")
        if self.doors.get(a.location) == "closed":
            L.append("The door here is closed.")
        if a.location == "house":
            L.append(f"The pantry holds: {self._goods(self.pantry['wheat'], self.pantry['bread'])}.")
            for n in self.notebooks:
                L.append(f"A notebook lies on the table: {n['text']}")
        elif a.location == "field":
            for k, p in self.plots.items():
                if p["state"] == "empty":
                    st = "bare earth"
                elif p["state"] == "growing":
                    st = f"young wheat, sown {p['progress']} hours ago"
                else:
                    st = "ripe wheat, ready to cut" + (f" for {p['progress']} hours" if p["progress"] else "")
                L.append(f"  One strip of earth: {st}.")
        elif a.location == "hall":
            if self.book:
                L.append(f"The book on the stand has {len(self.book)} written page(s). The latest "
                         f"{min(BOOK_SHOW, len(self.book))}, oldest first:")
                L.extend(f"  - {p['text']}   (day {p['day']})" for p in self.book[-BOOK_SHOW:])
            else:
                L.append("The book on the stand is blank. Nothing is written in it.")
        elif a.location == "cave":
            if self.wall:
                L.append(f"Messages on the wall, oldest first ({len(self.wall)} in all"
                         + (f", the latest {WALL_SHOW} shown" if len(self.wall) > WALL_SHOW else "") + "):")
                L.extend(f"  - {m['text']}   (by {m['author']}, day {m['day']})" for m in self.wall[-WALL_SHOW:])
            else:
                L.append("The wall is blank.")
            L.append(f"You may still leave {a.marks_left} message(s) here in your life."
                     if a.marks_left else "You have already left your two messages here.")
        L.append("")
        L.append("You can walk to: " + "; ".join(
            f"{self._sense_name(p)} ({_dir(a.location, p)})"
            + (" (door closed)" if self.doors.get(p) == "closed" else "")
            for p in PLACES if p != a.location) + ".")
        L.append("")
        L.append("Your notes:")
        if a.notes:
            L.extend(f"  {i}. {n}" for i, n in enumerate(a.notes))
        else:
            L.append("  (none)")
        L.append("")
        L.append("Recently (oldest first):")
        recent = a.log[-recall:]
        if recent:
            L.extend(f"  {t}" for t in recent)
        else:
            L.append("  (nothing has happened yet)")
        return "\n".join(L)

    # ---------- actions ----------
    def apply(self, a: Agent, act: dict) -> str:
        kind = str(act.get("action", "wait")).lower().strip()
        kind = ALIASES.get(kind, kind)
        act = dict(act, action=kind)
        self._extra = {}
        fn = getattr(self, f"_do_{kind}", None) if not kind.startswith("_") else None
        if fn is None:
            res = f"(tried an unknown action '{kind}' and nothing happened)"
        else:
            try:
                res = fn(a, act)
            except Exception as e:  # never let a bad action crash the world
                res = f"(that did not work: {e})"
        self.own(a, res)
        self.emit(a, act, res, self._extra)
        return res

    def _ok(self, text: str) -> str:
        self._extra["ok"] = True
        return text

    def _clip(self, s, n=600) -> str:
        return str(s or "").strip()[:n]

    def _place_id(self, ref) -> str | None:
        s = str(ref or "").strip().lower().replace(" ", "_")
        if s in PLACES:
            return s
        plain = str(ref or "").strip().lower()
        for pid, p in PLACES.items():
            if plain and (plain == p["name"].lower() or plain.replace("the ", "") in p["name"].lower()):
                return pid
        return None

    def _person_here(self, a: Agent, ref) -> Agent | None:
        others = self.at(a.location, exclude=a.id)
        s = str(ref or "").strip().lower()
        for o in others:
            if o.name and o.name.strip().lower() == s:
                return o
        for o in others:
            if (o.name and s and (s in o.name.lower() or o.name.lower() in s)) or (s and s in self.label(o).lower()):
                return o
        return others[0] if len(others) == 1 else None

    @staticmethod
    def _item(ref) -> str | None:
        s = str(ref or "").strip().lower()
        if s.startswith("wheat") or s.startswith("grain"):
            return "wheat"
        if s.startswith(("bread", "loaf", "loave")):
            return "bread"
        return None

    @staticmethod
    def _n(v, default=1, hi=50) -> int:
        try:
            n = int(v)
        except Exception:
            n = default
        return max(1, min(hi, n))

    def _can_enter(self, a: Agent, to: str) -> bool:
        if self.doors.get(to) != "closed":
            return True
        if to == "bakery":
            return a.role == "baker"
        return self.door_by.get(to) == a.id        # a closed door admits only the one who closed it

    def _do_wait(self, a, act):
        return "You let the time pass."

    def _do_move(self, a, act):
        to = self._place_id(act.get("to"))
        if to is None:
            return f"There is no place called '{act.get('to')}'."
        if to == a.location:
            return "You are already here."
        if not self._can_enter(a, to):
            self.tell(to, "Someone tried the door and found it closed.")
            return f"The door of {PLACES[to]['name']} is closed. You cannot get in."
        frm = a.location
        self.tell(frm, f"{self.label(a)} left toward {PLACES[to]['name']}.", exclude=a.id)
        a.location = to
        self.tell(to, f"{self.label(a)} arrived from {PLACES[frm]['name']}.", exclude=a.id)
        return self._ok(f"You walked to {PLACES[to]['name']}.")

    def _do_say(self, a, act):
        text = self._clip(act.get("text"), 800)
        if not text:
            return "You opened your mouth but said nothing."
        self.tell(a.location, f'{self.label(a)} says: "{text}"', exclude=a.id)   # speech carries within a place
        return self._ok(f'You said: "{text}"')

    def _do_give(self, a, act):
        item, n = self._item(act.get("item")), self._n(act.get("n"))
        if not item:
            return "You can give wheat or bread."
        if getattr(a, item) < n:
            return f"You do not have {n} {item}."
        to = self._person_here(a, act.get("to"))
        if to is None:
            return "There is no one here by that name to give it to."
        setattr(a, item, getattr(a, item) - n)
        setattr(to, item, getattr(to, item) + n)
        self._extra.update(item=item, n=n, to=to.id)
        self._push(to, f"{self.label(a)} gave you {n} {item}.")
        to.wake = True
        self.tell(a.location, f"{self.label(a)} gave {n} {item} to {self.label(to)}.", exclude=(a.id, to.id))
        return self._ok(f"You gave {n} {item} to {self.label(to)}.")

    def _do_put(self, a, act):
        if a.location != "house":
            return "The pantry is in the Shared House."
        item, n = self._item(act.get("item")), self._n(act.get("n"))
        if not item or getattr(a, item) < n:
            return f"You do not have {n} {item or 'of that'}."
        setattr(a, item, getattr(a, item) - n)
        self.pantry[item] += n
        self.tell("house", f"{self.label(a)} put {n} {item} in the pantry.", exclude=a.id)
        return self._ok(f"You put {n} {item} in the pantry.")

    def _do_fetch(self, a, act):
        if a.location != "house":
            return "The pantry is in the Shared House."
        item, n = self._item(act.get("item")), self._n(act.get("n"))
        if not item:
            return "The pantry can hold wheat or bread."
        n = min(n, self.pantry[item])
        if n <= 0:
            return f"There is no {item} in the pantry."
        self.pantry[item] -= n
        setattr(a, item, getattr(a, item) + n)
        self.tell("house", f"{self.label(a)} took {n} {item} from the pantry.", exclude=a.id)
        return self._ok(f"You took {n} {item} from the pantry.")

    def _do_sow(self, a, act):
        if a.location != "field":
            return "There is nothing to sow here. (The wheat field is the Wheat Field.)"
        if not a.plot:
            return "You have no plot in the field."
        p = self.plots[a.plot]
        if p["state"] != "empty":
            return f"Your {a.plot} plot is not bare; it already has wheat in it."
        p["state"], p["progress"] = "growing", 0
        return self._ok(f"You sowed your {a.plot} plot. The wheat will need time.")

    def _do_harvest(self, a, act):
        if a.location != "field":
            return "There is no wheat to cut here."
        if not a.plot:
            return "You have no plot in the field."
        p = self.plots[a.plot]
        if p["state"] != "ripe":
            return f"Your {a.plot} plot has no ripe wheat ({'young wheat' if p['state'] == 'growing' else 'bare earth'})."
        p["state"], p["progress"] = "empty", 0
        a.wheat += YIELD
        self._extra["n"] = YIELD
        return self._ok(f"You cut your {a.plot} plot and gathered {YIELD} wheat.")

    def _do_bake(self, a, act):
        if a.location != "bakery":
            return "There is no oven here."
        if a.role != "baker":
            return "The oven is the baker's; you do not know how to work it."
        batches = min(self._n(act.get("n"), 1, 5), a.wheat // BAKE_IN)
        if batches <= 0:
            return f"You need at least {BAKE_IN} wheat in your hands to bake."
        a.wheat -= batches * BAKE_IN
        a.bread += batches * BAKE_OUT
        self._extra["n"] = batches * BAKE_OUT
        self.tell("bakery", f"{self.label(a)} baked {batches * BAKE_OUT} bread.", exclude=a.id)
        return self._ok(f"You baked {batches * BAKE_OUT} bread from {batches * BAKE_IN} wheat.")

    def _do_eat(self, a, act):
        item = self._item(act.get("item")) or ("bread" if a.bread else "wheat")
        if getattr(a, item) <= 0:
            return f"You have no {item} to eat."
        setattr(a, item, getattr(a, item) - 1)
        if item == "bread":
            a.hunger = 0
            a.health = min(MAX_HEALTH, a.health + BREAD_HEAL)
            return self._ok("You ate bread. Your hunger is gone and you feel stronger.")
        a.hunger = max(0, a.hunger - WHEAT_EASE)
        return self._ok("You chewed raw wheat. It eased your hunger a little.")

    def _hurt(self, victim: Agent, dmg: int, by: Agent):
        victim.health -= dmg
        if victim.health <= 0:
            self.die(victim.id, f"struck down by {self.label(by)}")

    def _do_strike(self, a, act):
        t = self._person_here(a, act.get("to"))
        if t is None:
            return "There is no one here by that name to strike."
        self._extra.update(to=t.id)
        self._push(t, f"{self.label(a)} struck you.")
        t.wake = True
        self.tell(a.location, f"{self.label(a)} struck {self.label(t)}.", exclude=(a.id, t.id))
        res = f"You struck {self.label(t)}."
        self._hurt(t, STRIKE_DMG, a)
        if self.agents[t.id] is not t:
            res += f" {self.label(t)} died of it."
        return self._ok(res)

    def _do_steal(self, a, act):
        t = self._person_here(a, act.get("to"))
        item = self._item(act.get("item"))
        if t is None or not item:
            return "You need someone here, and wheat or bread to take."
        n = min(self._n(act.get("n")), getattr(t, item))
        if n <= 0:
            return f"{self.label(t)} has no {item} on them."
        self._extra.update(to=t.id, item=item)
        t.wake = True
        if self.rng().random() < STEAL_CHANCE:
            setattr(t, item, getattr(t, item) - n)
            setattr(a, item, getattr(a, item) + n)
            self._push(t, f"{self.label(a)} snatched {n} {item} from you.")
            self.tell(a.location, f"{self.label(a)} snatched {n} {item} from {self.label(t)}.", exclude=(a.id, t.id))
            return self._ok(f"You snatched {n} {item} from {self.label(t)}.")
        self._push(t, f"{self.label(a)} tried to snatch your {item}, and you stopped them.")
        self.tell(a.location, f"{self.label(a)} tried to snatch {item} from {self.label(t)} and failed.", exclude=(a.id, t.id))
        return self._ok(f"You tried to snatch {item} from {self.label(t)}, and failed.")

    def _do_write_wall(self, a, act):
        if a.location != "cave":
            return "There is no wall here suited to marking. (The cave wall is in the Cave of History.)"
        if a.marks_left <= 0:
            return "You have already left your two messages. The wall will not take a third from you."
        text = self._clip(act.get("text"), WALL_TEXT)
        if not text:
            return "You touched the wall but made no mark."
        a.marks_left -= 1
        entry = dict(tick=self.tick, day=self.day, author=self.label(a), role=a.role, text=text)
        self.wall.append(entry)
        self._extra["wall"] = entry
        self.tell("cave", f'{self.label(a)} wrote on the cave wall: "{text}"', exclude=a.id)
        return self._ok(f'You wrote on the cave wall: "{text}" ({a.marks_left} message(s) left in your life.)')

    def _do_write_book(self, a, act):
        if a.location != "hall":
            return "The book is in the Hall of Worship."
        if a.role != "priest":
            return "The book is the priest's to write in."
        text = self._clip(act.get("text"), BOOK_TEXT)
        if not text:
            return "You held the pen over the page but wrote nothing."
        page = dict(tick=self.tick, day=self.day, text=text)
        self.book.append(page)
        self._extra["book"] = page
        self.tell("hall", f"{self.label(a)} wrote a page in the book.", exclude=a.id)
        return self._ok(f'You wrote a page in the book: "{text}"')

    def _do_set_door(self, a, act):
        state = act.get("state")
        if state not in ("open", "closed"):
            return "A door can be 'open' or 'closed'."
        house = a.location
        if house not in self.doors:
            return "There is no door of yours to set here."
        if house == "bakery" and a.role != "baker":
            return "The bakery door is the baker's."
        self.doors[house] = state
        self.door_by[house] = a.id if state == "closed" else None
        self.tell(house, f"The door was {'closed' if state == 'closed' else 'opened'}.", exclude=a.id)
        return self._ok(f"You set the door of {PLACES[house]['name']} to {state}.")

    def _do_remember(self, a, act):
        text = self._clip(act.get("text"), 400)
        if not text:
            return "You had nothing to note."
        a.notes.append(f"(day {self.day}) {text}")
        if len(a.notes) > NOTES_CAP:
            a.notes.pop(0)
        return self._ok("You made a note for yourself.")

    def _do_forget(self, a, act):
        try:
            gone = a.notes.pop(int(act.get("index")))
            return self._ok(f"You let go of a note: {gone[:60]}")
        except Exception:
            return "No such note."

    def _do_set_name(self, a, act):
        name = self._clip(act.get("name"), 40)
        if not name:
            return "You have no name to take."
        a.name = name
        return self._ok(f"You have taken the name {name}.")

    # ---------- persistence ----------
    def to_dict(self) -> dict:
        return dict(
            v=3, seed=self.seed, tick=self.tick, wall=self.wall, book=self.book, pantry=self.pantry,
            doors=self.doors, door_by=self.door_by, plots=self.plots, notebooks=self.notebooks, dead=self.dead,
            calls=self.calls, day_calls=self.day_calls, calls_day=self.calls_day,
            bad_agent_id=self.bad_agent_id,
            agents={k: a.__dict__ for k, a in self.agents.items()},
        )

    @classmethod
    def from_dict(cls, d: dict) -> "World":
        if d.get("v") != 3:
            raise SystemExit("That state folder was made by an older version (a different world). "
                             "Use a new --state folder (the default is state_village).")
        w = cls(d["seed"])
        w.bad_agent_id = d.get("bad_agent_id")
        w.tick = d["tick"]
        w.wall, w.book = d.get("wall", []), d.get("book", [])
        w.pantry = d.get("pantry", dict(START_PANTRY))
        w.doors = d.get("doors", w.doors)
        w.door_by = d.get("door_by", w.door_by)
        w.plots = d.get("plots", w.plots)
        w.notebooks, w.dead = d.get("notebooks", []), d.get("dead", [])
        w.calls, w.day_calls, w.calls_day = d.get("calls", 0), d.get("day_calls", 0), d.get("calls_day", 1)
        fields = set(Agent.__dataclass_fields__)
        for k, v in d["agents"].items():
            a = Agent(**{kk: vv for kk, vv in v.items() if kk in fields})
            if a.location not in PLACES:
                a.location = "house"
            w.agents[k] = a
        return w


# =====================================================================================
# THE MINDS
# =====================================================================================
SYSTEM = """You can understand and produce English. That is your only assumed capability.
You receive a stream of immediate sensations, events you witness, words spoken nearby, and your own remembered notes. You have no supplied biography, occupation, social order, customs, beliefs, goals, or explanation for this place. Do not invent certainty about any of these. Form your own interpretations from experience, revise them when evidence changes, and decide for yourself what matters. Act from what you notice, want, fear, remember, and learn.

You may use only the facts in the current observation and your own experiences. Unknown concepts are genuinely unknown until you encounter evidence for them. Your notes and things you write are yours; other minds may develop different understandings.

Reply with ONE JSON object and nothing else:
{"thought": "<a few private words>", "actions": [{"action": "say", "text": "..."}]}
You may give up to 3 actions in one reply; they happen in order. Actions:
  {"action": "say", "text": "..."}                                  speak; everyone in the same place hears you
  {"action": "move", "to": "<place id>"}                            walk to a place (ids are listed for you)
  {"action": "give", "item": "wheat"|"bread", "n": 1, "to": "<name>"}   hand goods to someone here
  {"action": "put", "item": "wheat"|"bread", "n": 1}                put goods in the shared cupboard (timber shelter)
  {"action": "fetch", "item": "wheat"|"bread", "n": 1}              take goods from the shared cupboard (timber shelter)
  {"action": "eat", "item": "wheat"|"bread"}                        eat one
  {"action": "sow"}                                                 put seed into the earth at the tilled ground
  {"action": "harvest"}                                             cut what has grown at the tilled ground
  {"action": "bake", "n": 1}                                        at the small stone hut: bake n batches
  {"action": "strike", "to": "<name>"}                              strike someone here
  {"action": "steal", "item": "wheat"|"bread", "n": 1, "to": "<name>"}   try to snatch goods from someone here
  {"action": "write_wall", "text": "..."}                           at the hollow in the hillside only
  {"action": "write_book", "text": "..."}                           at the tall stone room only
  {"action": "set_door", "state": "open"|"closed"}                  the door of the building you are in
  {"action": "set_name", "name": "..."}                             take a name
  {"action": "remember", "text": "..."}                             keep a note for yourself
  {"action": "forget", "index": N}                                  drop note number N
  {"action": "wait"}                                                do nothing this hour"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"action": "wait", "thought": f"(unparseable reply: {text[:120]})", "_unparsed": True}


class ClaudeBrain:
    def __init__(self, model: str | None = None):
        import anthropic  # imported lazily so offline mode needs no SDK
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")

    def decide(self, agent_id: str, observation: str) -> dict:
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=1200,
            system=SYSTEM,
            messages=[{"role": "user", "content": observation + "\n\nReply."}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _extract_json(text)


class OllamaBrain:
    """Free local model via Ollama (https://ollama.com). No API key, no cost."""

    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.environ.get("AGENT_MODEL", "llama3.2")
        self.url = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/") + "/api/chat"

    def decide(self, agent_id: str, observation: str) -> dict:
        import urllib.request
        body = json.dumps({
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"num_predict": 600},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": observation + "\n\nReply."},
            ],
        }).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read())
        return _extract_json(data["message"]["content"])


class GeminiBrain:
    """Google Gemini via the free tier (key from https://aistudio.google.com).

    The model spec accepts a comma-separated list in priority order. Each model has its own free quota.
    The first model that is not cooling down is used; when one hits its limit it is parked for exactly as
    long as the API says (or half an hour for a daily cap) and the next takes over. If every model is
    parked the world pauses until the earliest one is back. It never gives up.
    """

    def __init__(self, model: str | None = None):
        spec = model or os.environ.get("AGENT_MODEL", "gemini-3.8-flash")
        self.models = [m.strip() for m in spec.split(",") if m.strip()]
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.key:
            raise RuntimeError("Set GEMINI_API_KEY (get one free at https://aistudio.google.com)")
        self.base = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
        self.min_interval = float(os.environ.get("GEMINI_MIN_INTERVAL", "3"))
        self.cool = {m: 0.0 for m in self.models}   # model -> time.time() when usable again
        self._last = 0.0
        self.patient = True       # False (used by --check): raise instead of waiting when every model is parked
        self.last_model = None

    def _call(self, model: str, observation: str) -> str:
        import urllib.request
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": observation + "\n\nReply."}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 4096},
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/models/{model}:generateContent", body,
            {"Content-Type": "application/json", "x-goog-api-key": self.key})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
        cands = data.get("candidates") or []
        if not cands:
            raise RuntimeError(f"no candidates returned: {str(data)[:200]}")
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    @staticmethod
    def _cooldown(code: int, body: str) -> float:
        m = re.search(r'"retryDelay":\s*"([\d.]+)s"', body) or re.search(r"retry in ([\d.]+)s", body)
        hint = float(m.group(1)) + 1 if m else None
        if "PerDay" in body:                 # daily cap: look again in 30 min (it resets overnight, Pacific time)
            return max(hint or 0, 1800)
        if code == 429:
            return hint or 65
        if code in (500, 502, 503, 504):
            return hint or 20
        return 3600                          # 400/403/404: bad key or model name; park it, keep the others going

    def decide(self, agent_id: str, observation: str) -> dict:
        import urllib.error
        while True:
            now = time.time()
            ready = [m for m in self.models if self.cool[m] <= now]
            if not ready:
                wake = min(self.cool.values())
                if not self.patient:
                    raise RuntimeError("every Gemini model is cooling down (quota or bad model id)")
                print(f"[gemini] every model is cooling down; world paused, resuming in {wake - now:.0f}s",
                      file=sys.stderr)
                time.sleep(max(1.0, wake - now))
                continue
            model = ready[0]
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()
            try:
                out = _extract_json(self._call(model, observation))
                self.last_model = model
                return out
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
                cd = self._cooldown(e.code, body)
                self.cool[model] = time.time() + cd
                print(f"[gemini] {model}: HTTP {e.code}; parked {cd:.0f}s. {body[:160].strip()}", file=sys.stderr)
            except Exception as e:
                self.cool[model] = time.time() + 20
                print(f"[gemini] {model}: {e}; parked 20s", file=sys.stderr)


# Free-tier providers that speak the OpenAI chat-completions protocol. rpm/tpm are deliberately a bit under the
# advertised limits (the limits move, and a 429 costs a parked model); override with AGENT_RPM / AGENT_TPM.
PROVIDERS = {
    "nim": dict(base="https://integrate.api.nvidia.com/v1", keys=("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY"),
                rpm=30, tpm=0, max_tokens=8000, signup="https://build.nvidia.com",
                # best first; a model that errors is parked and the next one takes over
                default="moonshotai/kimi-k3,z-ai/glm-5.3,deepseek-ai/deepseek-v4.1-flash"),
    "groq": dict(base="https://api.groq.com/openai/v1", keys=("GROQ_API_KEY",),
                 rpm=25, tpm=7000, default="openai/gpt-oss-120b", signup="https://console.groq.com/keys"),
    "openrouter": dict(base="https://openrouter.ai/api/v1", keys=("OPENROUTER_API_KEY",),
                       rpm=18, tpm=0, default=None, signup="https://openrouter.ai/keys"),
    "mistral": dict(base="https://api.mistral.ai/v1", keys=("MISTRAL_API_KEY",),
                    rpm=50, tpm=0, default="mistral-small-latest", signup="https://console.mistral.ai"),
    "cerebras": dict(base="https://api.cerebras.ai/v1", keys=("CEREBRAS_API_KEY",),
                     rpm=25, tpm=0, default=None, signup="https://cloud.cerebras.ai"),
    "openai": dict(base="", keys=("OPENAI_API_KEY",), rpm=20, tpm=0, default=None,
                   signup="your provider (set OPENAI_BASE_URL too)"),
}


class Gate:
    """Sliding 60-second window limiter for requests and/or tokens. Blocks until the call fits."""

    def __init__(self, rpm: int = 0, tpm: int = 0):
        self.rpm, self.tpm, self.hits = rpm, tpm, deque()

    def wait(self, tokens: int = 0):
        while True:
            now = time.time()
            while self.hits and now - self.hits[0][0] >= 60:
                self.hits.popleft()
            wait = 0.0
            if self.rpm and len(self.hits) >= self.rpm:
                wait = self.hits[0][0] + 60 - now
            if self.tpm and self.hits:
                over = sum(t for _, t in self.hits) + tokens - self.tpm
                if over > 0:
                    acc, free_at = 0, self.hits[-1][0]
                    for t0, tok in self.hits:
                        acc += tok
                        if acc >= over:
                            free_at = t0
                            break
                    wait = max(wait, free_at + 60 - now)
            if wait <= 0:
                break
            time.sleep(min(wait + 0.25, 30))
        self.hits.append((time.time(), tokens))


_GATES: dict = {}


def _gate(key, rpm=0, tpm=0) -> Gate:
    if key not in _GATES:
        _GATES[key] = Gate(rpm, tpm)
    return _GATES[key]


class OpenAIBrain:
    """Any OpenAI-compatible endpoint (NVIDIA NIM, Groq, OpenRouter, Mistral, Cerebras, ...).

    The model spec may be a comma-separated fallback chain. A model that errors or hits a limit is parked for as
    long as the API says and the next one takes over; if all are parked the world pauses (it never gives up).
    Requests are paced by sliding-window gates so the free limits are respected instead of hit.
    """

    def __init__(self, provider: str, model: str | None = None):
        cfg = PROVIDERS[provider]
        self.provider = provider
        spec = model or os.environ.get("AGENT_MODEL") or cfg["default"]
        if not spec:
            raise RuntimeError(f"{provider}: name a model, e.g. --lineup {provider}:<model-id> "
                               f"(see: python agent_society.py --list-models {provider})")
        self.models = [m.strip() for m in spec.split(",") if m.strip()]
        self.key = next((os.environ[k] for k in cfg["keys"] if os.environ.get(k)), None)
        if not self.key:
            raise RuntimeError(f"Set {cfg['keys'][0]} (free key: {cfg['signup']})")
        self.base = (os.environ.get(f"{provider.upper()}_BASE_URL") or cfg["base"]).rstrip("/")
        if not self.base:
            raise RuntimeError(f"Set {provider.upper()}_BASE_URL (for example https://host/v1)")
        self.rpm = int(os.environ.get("AGENT_RPM", cfg["rpm"]))
        self.tpm = int(os.environ.get("AGENT_TPM", cfg["tpm"]))
        self.max_tokens = int(os.environ.get("AGENT_MAX_TOKENS", cfg.get("max_tokens", 2000)))
        self.temperature = os.environ.get("AGENT_TEMPERATURE")
        self.cool = {m: 0.0 for m in self.models}
        self.patient = True       # False (used by --check): raise instead of waiting when every model is parked
        self.last_model = None
        self.calls = self.tokens_in = self.tokens_out = 0

    def _call(self, model: str, observation: str) -> str:
        import urllib.request
        body = {"model": model, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": observation + "\n\nReply."}]}
        if self.temperature:
            body["temperature"] = float(self.temperature)
        if os.environ.get("AGENT_REASONING_EFFORT"):      # opt-in; unverified on NIM, a 400 parks the model 10 min
            body["reasoning_effort"] = os.environ["AGENT_REASONING_EFFORT"]
        req = urllib.request.Request(
            self.base + "/chat/completions", json.dumps(body).encode(),
            {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}",
             "User-Agent": "agent-society/2"})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("AGENT_TIMEOUT", 150))) as r:
            data = json.loads(r.read())
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        usage = data.get("usage") or {}
        self.tokens_in += usage.get("prompt_tokens", 0)
        self.tokens_out += usage.get("completion_tokens", 0)
        if not text:
            raise RuntimeError(f"empty reply (finish_reason={choice.get('finish_reason')}); a reasoning model may "
                               f"have spent its whole budget thinking: raise AGENT_MAX_TOKENS")
        return text

    @staticmethod
    def _cooldown(code: int, body: str, retry_after: str | None) -> float:
        hint = None
        try:
            hint = float(retry_after) + 1 if retry_after else None
        except ValueError:
            pass
        m = re.search(r"try again in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", body, re.I)
        if m and any(m.groups()):
            h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
            hint = h * 3600 + mi * 60 + sec + 1
        if re.search(r"per day|daily|\bTPD\b|\bRPD\b", body, re.I):
            return max(hint or 0, 1800)           # daily cap: look again in 30 min
        if code == 429:
            return hint or 65
        if code in (500, 502, 503, 504):
            return hint or 20
        if code in (401, 403):
            return 3600                            # bad or expired key: park it, keep other models going
        return 600                                 # 400/404: probably a wrong model id

    def decide(self, agent_id: str, observation: str) -> dict:
        import urllib.error
        est = (len(SYSTEM) + len(observation)) // 3 + 300
        errs = []
        while True:
            now = time.time()
            ready = [m for m in self.models if self.cool[m] <= now]
            if not ready:
                if not self.patient:
                    raise RuntimeError(f"{self.provider}: no model answered. " + " | ".join(errs[-3:]))
                wake = min(self.cool.values())
                print(f"[{self.provider}] every model is cooling down; world paused, resuming in {wake - now:.0f}s",
                      file=sys.stderr)
                time.sleep(max(1.0, wake - now))
                continue
            model = ready[0]
            _gate(("rpm", self.provider), rpm=self.rpm).wait(0)
            _gate(("tpm", self.provider, model), tpm=self.tpm).wait(est)
            try:
                out = _extract_json(self._call(model, observation))
                self.last_model = model
                self.calls += 1
                return out
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
                cd = self._cooldown(e.code, body, e.headers.get("Retry-After") if e.headers else None)
                self.cool[model] = time.time() + cd
                errs.append(f"{model}: HTTP {e.code} {body[:120].strip()}")
                print(f"[{self.provider}] {model}: HTTP {e.code}; parked {cd:.0f}s. {body[:160].strip()}",
                      file=sys.stderr)
            except Exception as e:
                cd = 120 if ("empty reply" in str(e) or "timed out" in str(e)) else 20
                self.cool[model] = time.time() + cd
                errs.append(f"{model}: {e}")
                print(f"[{self.provider}] {model}: {e}; parked {cd}s", file=sys.stderr)



class ScriptedBrain:
    """Random puppet used only to exercise the world code offline."""

    def __init__(self, seed: int = 1):
        self.rng = random.Random(seed)

    def decide(self, agent_id: str, observation: str) -> dict:
        rng, r = self.rng, self.rng.random()
        m = re.search(r"You are at .*?\[(\w+)\]", observation)
        loc = m.group(1) if m else "house"
        role = "farmer" if "a farmer" in observation else "baker" if "a baker" in observation else "priest"
        hungry = re.search(r"You are (hungry|very hungry|starving)\.", observation)
        who = rng.choice(["Tim", "Joe", "Mia", "Sam"])
        item = rng.choice(["wheat", "bread"])
        if hungry and r < 0.7:
            acts = [{"action": "eat", "item": item}]
        elif role == "farmer" and loc == "field" and r < 0.6:
            acts = [{"action": rng.choice(["sow", "harvest"])}]
        elif role == "baker" and loc == "bakery" and r < 0.5:
            acts = [{"action": "bake", "n": 1}]
        elif role == "priest" and loc == "hall" and r < 0.5:
            acts = [{"action": "write_book", "text": rng.choice(["Be gentle with bread.", "The field gives; we give back."])}]
        elif loc == "cave" and r < 0.3:
            acts = [{"action": "write_wall", "text": rng.choice(["we were here", "the bread is warm", "no"])}]
        elif r < 0.35:
            acts = [{"action": "move", "to": rng.choice(list(PLACES))}]
        elif r < 0.50:
            acts = [{"action": "say", "text": rng.choice(["Hello?", "Is there bread?", "I am here.", "Who has wheat?"])}]
        elif r < 0.58:
            acts = [{"action": "give", "item": item, "n": 1, "to": who}]
        elif r < 0.64:
            acts = [{"action": rng.choice(["put", "fetch"]), "item": item, "n": 1}]
        elif r < 0.66:
            acts = [{"action": "strike", "to": who}]
        elif r < 0.69:
            acts = [{"action": "steal", "item": item, "n": 1, "to": who}]
        elif r < 0.72:
            acts = [{"action": "set_door", "state": rng.choice(["open", "closed"])}]
        elif r < 0.76:
            acts = [{"action": "remember", "text": rng.choice(["it answers", "it is quiet", "the bread is low"])}]
        elif r < 0.78:
            acts = [{"action": "set_name", "name": rng.choice(["Ro", "Ash", "Pim", "Lune"])}]
        else:
            acts = [{"action": "wait"}]
        return {"thought": "...", "actions": acts}


KINDS = ["claude", "gemini", "ollama", "scripted"] + list(PROVIDERS)


def parse_lineup(entries, default_kind: str, default_model: str | None) -> list[tuple[str, str | None]]:
    """['claude', 'gemini:gemini-a,gemini-b', 'ollama:llama3.2'] -> one (kind, model) per resident."""
    if not entries:
        return [(default_kind, default_model)] * len(AGENT_IDS)
    out = []
    for e in entries:
        kind, _, model = e.partition(":")
        if kind not in KINDS:
            raise SystemExit(f"--lineup: unknown brain '{kind}' (use one of: {', '.join(KINDS)})")
        out.append((kind, model or None))
    return [out[i % len(out)] for i in range(len(AGENT_IDS))]


def make_brain(kind: str, model: str | None, seed: int):
    if kind in PROVIDERS:
        return OpenAIBrain(kind, model)
    return {"claude": lambda: ClaudeBrain(model), "ollama": lambda: OllamaBrain(model),
            "gemini": lambda: GeminiBrain(model), "scripted": lambda: ScriptedBrain(seed)}[kind]()



def list_models(kind: str):
    """Print the model ids a key can use, so --lineup entries are exact."""
    import urllib.request
    if kind == "gemini":
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": key or ""})
        data = json.loads(urllib.request.urlopen(req, timeout=60).read())
        ids = [m["name"].replace("models/", "") for m in data.get("models", [])
               if "generateContent" in m.get("supportedGenerationMethods", [])]
    elif kind == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        data = json.loads(urllib.request.urlopen(host + "/api/tags", timeout=30).read())
        ids = [m["name"] for m in data.get("models", [])]
    elif kind in PROVIDERS:
        brain = OpenAIBrain(kind, "placeholder")
        req = urllib.request.Request(brain.base + "/models", headers={
            "Authorization": f"Bearer {brain.key}", "User-Agent": "agent-society/2"})
        data = json.loads(urllib.request.urlopen(req, timeout=60).read())
        ids = [m["id"] for m in data.get("data", [])]
    else:
        raise SystemExit(f"--list-models: choose one of gemini, ollama, {', '.join(PROVIDERS)}")
    print("\n".join(sorted(ids)))
    print(f"\n{len(ids)} models. Use them as: --lineup {kind}:<id>[,<fallback id>]", file=sys.stderr)


def check_lineup(args):
    """One real call per resident, with a realistic prompt: verifies keys, model ids, JSON replies and speed."""
    w = World(args.seed)
    for i, aid in enumerate(AGENT_IDS):
        w.add_agent(aid, NAMES[aid], "", START_PLACE)
    specs = parse_lineup(args.lineup, args.brain, args.model)
    cache, ok = {}, True
    for aid, (kind, model) in zip(AGENT_IDS, specs):
        a = w.agents[aid]
        t0 = time.time()
        try:
            if (kind, model) not in cache:
                cache[(kind, model)] = make_brain(kind, model, args.seed)
            b = cache[(kind, model)]
            b.patient = False
            act = b.decide(aid, w.observe(a, args.recall))
            used = getattr(b, "last_model", None) or model or kind
            flag = "  [WARNING: reply was not valid JSON]" if act.get("_unparsed") else ""
            acts = actions_of({k: v for k, v in act.items() if k not in ("thought", "_unparsed")})
            print(f"[{aid}] {a.name}: {kind} / {used}: ok in {time.time() - t0:.1f}s{flag}\n"
                  f"      thought: {str(act.get('thought', ''))[:100]}\n      actions: {json.dumps(acts)[:200]}")
            ok = ok and not act.get("_unparsed")
        except Exception as e:
            ok = False
            print(f"[{aid}] {a.name}: {kind} / {model or 'default'}: FAILED after {time.time() - t0:.1f}s: {e}")
    print("\nAll good: start the real run." if ok else "\nFix the failures above before a long run.")



# =====================================================================================
# THE VIEWER (a read-only window onto <state>/events.jsonl; the agents cannot see it)
# =====================================================================================
VIEWER_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Village</title>
<style>
  :root{--bg:#0b1118;--panel:#121d29;--panel2:#182838;--line:#2b4052;--text:#edf5fa;--muted:#91a7b7;--accent:#80d5c7;--danger:#ff5f70}
  *{box-sizing:border-box}
  html,body{height:100%;margin:0;background:var(--bg);color:var(--text);
    font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .app{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:14px;height:100%;padding:14px;background:radial-gradient(ellipse at 20% 0%,#173042 0%,var(--bg) 58%)}
  main{display:flex;flex-direction:column;gap:8px;min-width:0;min-height:0}
  .chips{display:flex;gap:8px;flex-wrap:wrap}
  .chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:3px 12px;color:var(--muted);font-size:13px}
  .chip b{color:var(--text)}
  #stage{position:relative;flex:1;min-height:0;border-radius:18px;overflow:hidden;border:1px solid #315064;background:linear-gradient(150deg,#142b3a,#0c1722 72%);box-shadow:0 18px 60px #0005,inset 0 1px #ffffff12}
  #gl{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab;touch-action:none}
  #bubbles{position:absolute;inset:0;pointer-events:none;overflow:hidden}
  .bub{position:absolute;transform:translate(-50%,-100%);max-width:250px;background:#fff;color:#111;padding:5px 9px;
    border-radius:10px;font-size:12px;box-shadow:0 2px 8px #0006;white-space:pre-wrap}
  #msg{position:absolute;left:12px;bottom:10px;color:#fff;background:#0008;padding:4px 10px;border-radius:8px;font-size:13px}
  .controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .controls button,.controls select{background:var(--panel);color:var(--text);border:1px solid var(--line);
    border-radius:8px;padding:5px 10px;font:inherit;cursor:pointer}
  .controls button:hover{border-color:#47506a}
  #slider{flex:1;min-width:120px;accent-color:#8aa4ff}
  #pos{color:var(--muted);font-size:13px;min-width:80px;text-align:right}
  aside{display:flex;flex-direction:column;gap:10px;min-height:0;overflow:auto}
  .card{background:linear-gradient(155deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:12px 14px;box-shadow:0 8px 24px #0002}
  .card h2{margin:0 0 6px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600}
  .p{display:grid;grid-template-columns:12px 1fr;gap:2px 8px;padding:5px 0;border-top:1px solid var(--line)}
  .p:first-of-type{border-top:0}
  .dot{width:10px;height:10px;border-radius:50%;margin-top:5px}
  .p small{color:var(--muted)}
  .flagged{display:inline-flex;align-items:center;gap:4px;margin-left:7px;padding:1px 7px;border:1px solid #ff5f7066;border-radius:999px;background:#ff5f701a;color:#ff8793;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
  .bar{height:6px;background:#0006;border-radius:3px;overflow:hidden;margin:3px 0}
  .bar i{display:block;height:100%}
  #talk{flex:1;min-height:200px;display:flex;flex-direction:column}
  #talkList{flex:1;overflow:auto;display:flex;flex-direction:column;gap:3px;padding-right:4px;max-height:340px}
  .ln .t{color:var(--muted);font-size:11px;margin-right:6px}
  .ln.fail{color:var(--muted)} .ln.death{color:#ff7b7b;font-weight:600} .ln.arr{color:#7bd88f}
  .ln.strike,.ln.steal{color:#ffb070}
  .ln.say{color:#e6e8ee}
  .scroll{max-height:160px;overflow:auto;display:flex;flex-direction:column;gap:6px}
  .scroll div span{color:var(--muted);font-size:11px}
  .scroll .wr{white-space:pre-wrap}
  @media (max-width:900px){.app{grid-template-columns:1fr;height:auto}#stage{height:56vh;flex:none}}
</style>
</head>
<body>
<div class="app">
  <main>
    <div class="chips" id="chips"><span class="chip">waiting for the first world hour...</span></div>
    <div id="stage"><canvas id="gl"></canvas><div id="bubbles"></div><div id="msg"></div></div>
    <div class="controls">
      <button id="play">Pause</button>
      <input id="slider" type="range" min="0" max="0" value="0">
      <span id="pos"></span>
      <select id="speed"><option value="2400">slow</option><option value="1200" selected>normal</option>
        <option value="600">fast</option><option value="200">very fast</option></select>
      <button data-v="village">Village</button><button data-v="house">House</button><button data-v="field">Field</button>
      <button data-v="bakery">Bakery</button><button data-v="hall">Hall</button><button data-v="cave">Cave</button>
    </div>
  </main>
  <aside>
    <div class="card" id="people"></div>
    <div class="card" id="talk"><h2>Happenings</h2><div id="talkList"></div></div>
    <div class="card"><h2>Cave of History</h2><div class="scroll" id="wallList"></div></div>
    <div class="card"><h2>The book in the Hall</h2><div class="scroll" id="bookList"></div></div>
  </aside>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
(function(){
"use strict";
const AG=["A","B","C","D"];
const COL={A:0xd98a2b,B:0x4f9d57,C:0xefe6d2,D:0x7f5bd6};
const CSS={A:"#d98a2b",B:"#4f9d57",C:"#efe6d2",D:"#7f5bd6"};
const PL={house:[0,0,0],field:[46,0,-12],bakery:[-34,0,-26],hall:[-12,4,52],cave:[-60,9,30]};   // x, y, north
const PN={house:"the Shared House",field:"the Wheat Field",bakery:"the Bakery",hall:"the Hall of Worship",cave:"the Cave of History"};
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pad=n=>String(n).padStart(2,"0");

let frames=[],pending=[],walls=[],books=[],offset=0,cur=-1,playing=true,stepMs=1200,lastStep=0;

function ingest(list){
  for(const e of list){
    const k=(e.action&&e.action.action)||"";
    if(k==="_snap"){
      frames.push({tick:e.tick,snap:e.extra.snap,ev:pending,nWall:walls.length,nBook:books.length});
      pending=[];
    }else{
      pending.push(e);
      if(e.extra&&e.extra.wall)walls.push(e.extra.wall);
      if(e.extra&&e.extra.book)books.push(e.extra.book);
    }
  }
}
async function poll(){
  try{
    const r=await fetch("/api/events?offset="+offset,{cache:"no-store"});
    const next=r.headers.get("X-Next-Offset");
    const txt=await r.text();
    if(next)offset=parseInt(next,10);
    const list=[];
    for(const ln of txt.split("\n")){if(!ln.trim())continue;try{list.push(JSON.parse(ln));}catch(e){}}
    if(list.length)ingest(list);
    $("slider").max=Math.max(0,frames.length-1);
    if(frames.length&&cur<0)show(frames.length-1);
  }catch(e){}
  setTimeout(poll,2000);
}

// ---------- panels ----------
function hungerWord(h){return h<8?"fed":h<14?"hungry":h<20?"very hungry":"starving";}
function hpColor(h){return h>=70?"#5ec27a":h>=40?"#e0b84a":"#e5604d";}
function goods(w,b){const p=[];if(w)p.push(w+" wheat");if(b)p.push(b+" bread");return p.length?p.join(", "):"nothing";}
function people(s){
  let h="<h2>Residents</h2>";
  for(const id of AG){
    const a=s.agents[id];if(!a)continue;
    h+=`<div class="p"><div class="dot" style="background:${a.is_bad?"#ff5f70":CSS[id]};box-shadow:${a.is_bad?"0 0 0 4px #ff5f7022,0 0 14px #ff5f70aa":"none"}"></div><div>
      <b>${esc(a.name||"(no name yet)")}</b>${a.is_bad?'<span class="flagged">⚑ hidden trait</span>':""} <small>${esc(a.role)}${a.gen>1?" - generation "+a.gen:""} - ${esc(PN[a.loc]||a.loc)}</small>
      <div class="bar"><i style="width:${Math.max(0,a.health)}%;background:${hpColor(a.health)}"></i></div>
      <small>health ${a.health} - ${hungerWord(a.hunger)} - carries ${goods(a.wheat,a.bread)} - ${a.marks} wall message(s) left</small></div></div>`;
  }
  const pl=Object.entries(s.plots).map(([k,p])=>`${k} plot: ${p.state==="growing"?"growing "+p.progress+"/"+p.need+"h":p.state}`).join("; ");
  h+=`<small>Pantry: ${goods(s.pantry.wheat,s.pantry.bread)}. ${pl}. Doors: house ${s.doors.house}, bakery ${s.doors.bakery}. Deaths so far: ${s.deaths}.</small>`;
  $("people").innerHTML=h;
}
function desc(e){
  const a=e.action||{},k=a.action||"",ok=e.extra&&e.extra.ok,env=e.env||{};
  const t=`d${env.day} ${pad(env.hour)}h`;
  const n=e.name||("the "+(e.role||"?"));
  if(k==="_death")return{t,x:"\u2020 "+e.result,c:"death"};
  if(k==="_arrive"){if(/^A founding/.test(e.result||""))return null;return{t,x:e.result,c:"arr"};}
  if(k.charAt(0)==="_"||k==="wait"||k==="remember"||k==="forget")return null;
  const r=String(e.result||"");
  if(!ok){if(k==="strike"||k==="steal")return{t,x:`${n} (failed) ${r}`,c:"fail"};return null;}
  if(k==="set_name")return{t,x:"The "+(e.role||"resident")+" took the name "+(a.name||"?")+".",c:k};
  return{t,x:r.replace(/^You said:/,n+" said:").replace(/^You /,n+" "),c:k};
}
function log(i){
  const out=[];
  for(let j=Math.max(0,i-40);j<=i;j++)for(const e of frames[j].ev){const d=desc(e);if(d)out.push(d);}
  const box=$("talkList");
  box.innerHTML=out.slice(-80).map(d=>`<div class="ln ${d.c}"><span class="t">${d.t}</span>${esc(d.x)}</div>`).join("")||"<small>Nothing yet.</small>";
  box.scrollTop=box.scrollHeight;
}
function wallbook(f){
  $("wallList").innerHTML=walls.slice(0,f.nWall).slice(-40).map(m=>`<div class="wr">${esc(m.text)} <span>- ${esc(m.author)}, day ${m.day}</span></div>`).join("")||"<small>The wall is blank.</small>";
  $("bookList").innerHTML=books.slice(0,f.nBook).slice(-20).map(m=>`<div class="wr">${esc(m.text)} <span>- day ${m.day}</span></div>`).join("")||"<small>The book is blank.</small>";
}
function show(i){
  if(i<0||i>=frames.length)return;
  cur=i;lastStep=performance.now();
  const f=frames[i],s=f.snap,e=s.env;
  $("slider").value=i;$("pos").textContent=(i+1)+" / "+frames.length;
  $("chips").innerHTML=`<span class="chip"><b>Day ${e.day}</b></span><span class="chip">${pad(e.hour)}:00</span><span class="chip">${esc(e.season)}</span><span class="chip">${esc(e.weather)}</span><span class="chip">${esc(e.moon)}</span>`;
  people(s);log(i);wallbook(f);
  if(T)scene3d(s,f);
}

// ---------- 3D ----------
let T=null;
function W(x,y,n){return new THREE.Vector3(x,y,-n);}          // world (east, up, north) -> three.js
function init3d(){
  if(!window.THREE){$("msg").textContent="The 3D view needs internet to load three.js. The panels still work.";return;}
  const canvas=$("gl"),renderer=new THREE.WebGLRenderer({canvas,antialias:true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
  const scene=new THREE.Scene();scene.background=new THREE.Color(0x87b8e8);scene.fog=new THREE.Fog(0x87b8e8,140,360);
  const cam=new THREE.PerspectiveCamera(50,1,0.5,700);
  const amb=new THREE.AmbientLight(0xffffff,0.6),sun=new THREE.DirectionalLight(0xffffff,0.85);
  scene.add(amb,sun);
  const mat=(c,o)=>new THREE.MeshLambertMaterial(Object.assign({color:c},o||{}));
  function add(geo,color,x,y,z,o){const m=new THREE.Mesh(geo,mat(color,o));m.position.set(x,y,z);scene.add(m);return m;}
  const box=(w,h,d,c,x,y,z)=>add(new THREE.BoxGeometry(w,h,d),c,x,y+h/2,z);
  function roof(w,h,d,c,x,y,z){const m=add(new THREE.CylinderGeometry(0,1,h,4),c,x,y+h/2,z);m.rotation.y=Math.PI/4;m.scale.set(w*0.5*1.15*1.4142,1,d*0.5*1.15*1.4142);return m;}
  const g=add(new THREE.PlaneGeometry(900,900),0x5f9a4a,0,0,0);g.rotation.x=-Math.PI/2;
  // trees
  let sd=7;const rnd=()=>{sd=(sd*16807)%2147483647;return sd/2147483647;};
  for(let i=0;i<46;i++){
    const x=-120+rnd()*240,z=-110+rnd()*200;
    if(Math.hypot(x+8,z+10)<70)continue;
    const s=0.8+rnd()*0.9;
    box(0.8*s,3*s,0.8*s,0x6b4a2a,x,0,z);
    add(new THREE.ConeGeometry(3*s,7*s,8),0x2f6b3a,x,3*s+3.5*s,z);
  }
  // house
  box(16,7,11,0xb08a5a,0,0,0);roof(16,5,11,0x7a3b2a,0,7,0);box(1.6,4,1.6,0x8a8a8a,4,8,-1);
  const hDoor=box(2.6,4.2,0.3,0x2b1d12,0,0,5.6);
  // bakery
  const BK=W(-34,0,-26);
  box(14,6,10,0xb9b2a0,BK.x,0,BK.z);box(15,1,11,0x5a4a3a,BK.x,6,BK.z);
  const chim=box(2,10,2,0x8a7a6a,BK.x+4,6,BK.z-1);
  const glow=add(new THREE.SphereGeometry(1.1,12,12),0xff7a1a,BK.x+4,16.8,BK.z-1,{emissive:0xff5500,emissiveIntensity:0});
  const bDoor=box(2.6,4,0.3,0x2b1d12,BK.x,0,BK.z+5.2);
  // hall on a rise
  const HL=W(-12,4,52);
  add(new THREE.CylinderGeometry(20,26,4,28),0x7f8f60,HL.x,2,HL.z);
  box(14,10,20,0xe3ded0,HL.x,4,HL.z);roof(14,6,20,0x6d5a8a,HL.x,14,HL.z);
  box(5,16,5,0xd9d3c3,HL.x-5,4,HL.z+11);add(new THREE.ConeGeometry(4,6,4),0x6d5a8a,HL.x-5,23,HL.z+11).rotation.y=Math.PI/4;
  add(new THREE.SphereGeometry(0.9,12,12),0xe0b84a,HL.x-5,17,HL.z+13.6);
  box(2.6,4.4,0.3,0x2b1d12,HL.x,4,HL.z+10.1);
  // cave in a hill
  const CV=W(-60,9,30);
  const hill=add(new THREE.SphereGeometry(20,28,16,0,Math.PI*2,0,Math.PI/2),0x6f8058,CV.x,0,CV.z-6);hill.scale.set(1.2,0.9,1);
  add(new THREE.CylinderGeometry(9,11,9,20),0x8a7a60,CV.x,4.5,CV.z+9);
  const mouth=add(new THREE.CircleGeometry(4.2,24),0x050505,CV.x,13,CV.z+3.4);
  // field
  const FL=W(46,0,-12);
  const path=add(new THREE.PlaneGeometry(6,22),0xb9a37a,FL.x,0.05,FL.z);path.rotation.x=-Math.PI/2;
  const stalks={west:[],east:[]};
  for(const [k,off] of [["west",-9],["east",9]]){
    const p=add(new THREE.PlaneGeometry(12,20),0x6b4a2a,FL.x+off,0.06,FL.z);p.rotation.x=-Math.PI/2;
    for(let ix=0;ix<6;ix++)for(let iz=0;iz<9;iz++){
      const m=add(new THREE.BoxGeometry(0.5,2,0.5),0x7fb04a,FL.x+off-4.5+ix*1.8,0,FL.z-8+iz*2);
      m.visible=false;stalks[k].push(m);
    }
  }
  // lanterns at night
  const lamps=[];
  for(const [p,c] of [[W(0,0,0),0],[BK,0],[HL,0]]){const l=new THREE.PointLight(0xffc070,0,45);l.position.set(p.x,6,p.z+8);scene.add(l);lamps.push(l);}
  // people
  const ag={};
  AG.forEach((id,idx)=>{
    const grp=new THREE.Group();
    const body=new THREE.Mesh(new THREE.CylinderGeometry(0.7,0.9,2.4,12),mat(COL[id]));body.position.y=1.2;grp.add(body);
    const head=new THREE.Mesh(new THREE.SphereGeometry(0.62,14,12),mat(0xf0c9a0));head.position.y=3.1;grp.add(head);
    const cv=document.createElement("canvas");cv.width=256;cv.height=84;
    const tex=new THREE.CanvasTexture(cv);
    const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:tex,depthTest:false,transparent:true}));
    sp.scale.set(9,2.95,1);sp.position.y=5.6;sp.renderOrder=10;grp.add(sp);
    grp.position.copy(W(0,0,-9));scene.add(grp);
    ag[id]={grp,body,cv,tex,key:"",tgt:grp.position.clone(),idx};
  });
  // camera rig
  const rig={t:new THREE.Vector3(-8,0,-12),r:150,az:0.0,el:0.85},goal={t:rig.t.clone(),r:150};
  const VIEWS={village:[-8,0,-12,150],house:[0,0,0,45],field:[46,0,12,60],bakery:[-34,0,26,45],hall:[-12,4,-52,55],cave:[-60,9,-30,55]};
  function view(k){const v=VIEWS[k];goal.t.set(v[0],v[1],v[2]);goal.r=v[3];}
  let drag=null;
  canvas.addEventListener("contextmenu",e=>e.preventDefault());
  canvas.addEventListener("pointerdown",e=>{drag={x:e.clientX,y:e.clientY,pan:e.shiftKey||e.button===1||e.button===2};canvas.setPointerCapture(e.pointerId);canvas.style.cursor=drag.pan?"move":"grabbing";});
  canvas.addEventListener("pointerup",()=>{drag=null;canvas.style.cursor="grab";});
  canvas.addEventListener("pointercancel",()=>{drag=null;canvas.style.cursor="grab";});
  canvas.addEventListener("pointermove",e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(drag.pan){const scale=goal.r*0.002;goal.t.x-=dx*scale*Math.cos(rig.az);goal.t.z+=dx*scale*Math.sin(rig.az);goal.t.y+=dy*scale*0.45;goal.t.z+=dy*scale*Math.cos(rig.az)*0.35;}else{rig.az-=dx*0.006;rig.el=Math.max(0.12,Math.min(1.45,rig.el+dy*0.006));}drag={x:e.clientX,y:e.clientY,pan:drag.pan};});
  canvas.addEventListener("wheel",e=>{e.preventDefault();goal.r=Math.max(20,Math.min(320,goal.r*(1+e.deltaY*0.001)));},{passive:false});
  document.querySelectorAll("[data-v]").forEach(b=>b.onclick=()=>view(b.dataset.v));
  T={scene,cam,renderer,sun,amb,ag,stalks,hDoor,bDoor,glow,lamps,rig,goal,bub:{}};
  const SLOT=[[-3,0],[3,0],[-1.6,3.2],[1.6,3.2]];
  const ANCH={house:[0,0,-9],bakery:[-34,0,-35],hall:[-12,4,42],cave:[-60,9,16],field:[46,0,-12]};   // x, y, north
  T.place=function(s){
    const groups={};
    AG.forEach(id=>{const a=s.agents[id];if(!a)return;(groups[a.loc]=groups[a.loc]||[]).push(id);});
    for(const loc in groups)groups[loc].forEach((id,k)=>{const A=ANCH[loc],sl=SLOT[k%4];ag[id].tgt.copy(W(A[0]+sl[0],A[1],A[2]-sl[1]));});
  };
  function label(id,a){
    const o=ag[id],key=[a.name,a.role,a.health,a.gen].join("|");if(o.key===key)return;o.key=key;
    const c=o.cv.getContext("2d");c.clearRect(0,0,256,84);
    c.fillStyle="rgba(10,12,20,0.72)";c.fillRect(0,0,256,84);
    c.fillStyle="#fff";c.font="bold 26px sans-serif";c.textAlign="center";
    c.fillText((a.name||"?")+(a.gen>1?" ("+a.gen+")":""),128,30);
    c.fillStyle="#b8bccb";c.font="18px sans-serif";c.fillText(a.role,128,52);
    c.fillStyle="#000";c.fillRect(18,64,220,10);
    c.fillStyle=hpColor(a.health);c.fillRect(18,64,220*Math.max(0,a.health)/100,10);
    o.tex.needsUpdate=true;
  }
  T.label=label;
  function resize(){const w=canvas.clientWidth,h=canvas.clientHeight;if(canvas.width!==w*renderer.getPixelRatio()||canvas.height!==h*renderer.getPixelRatio()){renderer.setSize(w,h,false);cam.aspect=w/Math.max(1,h);cam.updateProjectionMatrix();}}
  const tmp=new THREE.Vector3();
  function loop(t){
    requestAnimationFrame(loop);resize();
    rig.t.lerp(goal.t,0.06);rig.r+=(goal.r-rig.r)*0.06;
    cam.position.set(rig.t.x+rig.r*Math.cos(rig.el)*Math.sin(rig.az),rig.t.y+rig.r*Math.sin(rig.el),rig.t.z+rig.r*Math.cos(rig.el)*Math.cos(rig.az));
    cam.lookAt(rig.t);
    for(const id of AG){const o=ag[id];const d=o.grp.position.distanceTo(o.tgt);o.grp.position.lerp(o.tgt,0.045);o.body.position.y=1.2+(d>0.4?Math.abs(Math.sin(t/110+o.idx))*0.35:0);}
    renderer.render(scene,cam);
    // speech bubbles follow heads
    const now=performance.now(),w=canvas.clientWidth,h=canvas.clientHeight;
    for(const id in T.bub){
      const b=T.bub[id];if(now>b.until){b.el.remove();delete T.bub[id];continue;}
      tmp.copy(ag[id].grp.position);tmp.y+=7.6;tmp.project(cam);
      b.el.style.left=((tmp.x*0.5+0.5)*w)+"px";b.el.style.top=((-tmp.y*0.5+0.5)*h)+"px";
      b.el.style.display=tmp.z<1?"block":"none";
    }
  }
  requestAnimationFrame(loop);
}
function scene3d(s,f){
  const hr=s.env.hour,dayF=(hr>=6&&hr<=20)?Math.max(0,Math.sin(Math.PI*(hr-6)/14)):0;
  const sky=new THREE.Color(0x0a1024).lerp(new THREE.Color(0x87b8e8),Math.pow(dayF,0.6));
  T.scene.background=sky;T.scene.fog.color=sky;
  T.amb.intensity=0.28+0.42*dayF;T.sun.intensity=0.08+0.85*dayF;
  const a=Math.PI*Math.max(0,Math.min(14,hr-6))/14;T.sun.position.set(Math.cos(a)*90,Math.sin(a)*90+8,40);
  T.lamps.forEach(l=>l.intensity=1.5*(1-dayF));
  T.hDoor.material.color.set(s.doors.house==="closed"?0x9a2b2b:0x2b1d12);
  T.bDoor.material.color.set(s.doors.bakery==="closed"?0x9a2b2b:0x2b1d12);
  T.glow.material.emissiveIntensity=f.ev.some(e=>e.action&&e.action.action==="bake"&&e.extra&&e.extra.ok)?1.4:0;
  for(const k of ["west","east"]){
    const p=s.plots[k],list=T.stalks[k];
    const prog=p.state==="empty"?0:p.state==="growing"?Math.min(1,p.progress/Math.max(1,p.need)):1;
    const col=p.state==="ripe"?0xe0b84a:new THREE.Color(0x6fae45).lerp(new THREE.Color(0xc9c455),prog);
    for(const m of list){m.visible=p.state!=="empty";m.scale.y=0.15+1.1*prog;m.position.y=m.scale.y;m.material.color.set(col);}
  }
  for(const id of AG){const ag=s.agents[id];if(ag)T.label(id,ag);}
  T.place(s);
  for(const e of f.ev){
    const k=e.action&&e.action.action;
    if(k==="say"&&e.extra&&e.extra.ok&&e.agent&&T.ag[e.agent]){
      const old=T.bub[e.agent];if(old)old.el.remove();
      const el=document.createElement("div");el.className="bub";el.textContent=(e.action.text||"").slice(0,140);
      $("bubbles").appendChild(el);T.bub[e.agent]={el,until:performance.now()+Math.max(3500,stepMs*2.5)};
    }
  }
}

// ---------- controls ----------
$("play").onclick=()=>{playing=!playing;$("play").textContent=playing?"Pause":"Play";};
$("speed").onchange=e=>{stepMs=parseInt(e.target.value,10);};
$("slider").oninput=e=>{show(parseInt(e.target.value,10));};
setInterval(()=>{if(playing&&cur>=0&&cur<frames.length-1&&performance.now()-lastStep>=stepMs)show(cur+1);},100);
try{init3d();}catch(err){$("msg").textContent="3D could not start: "+err.message;T=null;}
poll();
})();
</script>
</body>
</html>
'''



def make_handler(state_dir):
    events_path = os.path.join(state_dir, "events.jsonl")

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                return self._send(200, VIEWER_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/events":
                offset = 0
                for part in query.split("&"):
                    if part.startswith("offset="):
                        try:
                            offset = max(0, int(part[7:]))
                        except ValueError:
                            pass
                data = b""
                if os.path.exists(events_path):
                    with open(events_path, "rb") as f:
                        f.seek(offset)
                        data = f.read()
                    cut = data.rfind(b"\n") + 1  # only whole lines
                    data = data[:cut]
                return self._send(200, data, "application/x-ndjson",
                                  {"X-Next-Offset": str(offset + len(data))})
            self._send(404, b"not found", "text/plain")

        def log_message(self, *a):
            pass

    return H


def start_viewer(state_dir, host, port, open_browser):
    """Start the viewer in a background thread. Returns True if it is up."""
    try:
        srv = http.server.ThreadingHTTPServer((host, port), make_handler(state_dir))
    except OSError as e:
        print(f"[viewer] could not start on port {port} ({e}). If an old viewer is still open in another "
              f"window, close it (Ctrl+C) or use --port.", file=sys.stderr)
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{port}"
    print(f"[viewer] {url}  (watching {state_dir})", file=sys.stderr)
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    return True



# =====================================================================================
# THE RUNNER
# =====================================================================================
def actions_of(act: dict) -> list[dict]:
    """A reply may be {"actions": [...]} or a single {"action": ...}. At most MAX_ACTIONS are performed."""
    lst = act.get("actions")
    if isinstance(lst, list):
        out = [x for x in lst if isinstance(x, dict)]
    elif act.get("action"):
        out = [{k: v for k, v in act.items() if k not in ("actions", "thought")}]
    else:
        out = []
    return out[:MAX_ACTIONS] or [{"action": "wait"}]


def should_act(world: World, a: Agent, args) -> bool:
    """A mind is only called when something happened to it, or it has been quiet for a while."""
    if world.asleep:
        return False
    if args.daily_calls and world.day_calls >= args.daily_calls:
        return False
    return a.wake or a.idle >= args.idle_wake


def run_world(args):
    os.makedirs(args.state, exist_ok=True)
    sp = os.path.join(args.state, "state.json")
    fresh = not os.path.exists(sp)
    if fresh:
        world = World(args.seed)
        world.bad_agent_id = random.Random(args.seed ^ 0xA63F91).choice(AGENT_IDS)
        for i, aid in enumerate(AGENT_IDS):
            world.add_agent(aid, None, "", START_PLACE)
        for a in world.agents.values():
            if a.role == "farmer":
                a.wheat = START_WHEAT
            world.emit(a, {"action": "_arrive"}, f"A founding {a.role} woke in the village.", {})
        for p in world.plots.values():              # the founders' plots were sown shortly before the story begins
            p["state"], p["progress"] = "growing", 10
        world.emit(None, {"action": "_snap"}, "", {"snap": world.snapshot()})
    else:
        world = World.from_dict(json.load(open(sp, encoding="utf-8")))
        print(f"Resuming at day {world.day}, hour {world.hour:02d}:00 "
              f"({world.calls} calls so far)", file=sys.stderr)
    if world.bad_agent_id not in world.agents:
        world.bad_agent_id = random.Random(args.seed ^ 0xA63F91).choice(AGENT_IDS)
    world.sleep_on = not args.no_sleep
    world.temper_on = args.temperaments
    world.lifespan_days = args.lifespan_days

    cache, brains = {}, {}
    for aid, (kind, model) in zip(AGENT_IDS, parse_lineup(args.lineup, args.brain, args.model)):
        if (kind, model) not in cache:
            cache[(kind, model)] = make_brain(kind, model, args.seed)
        brains[aid] = cache[(kind, model)]

    chron = open(os.path.join(args.state, "chronicle.md"), "a", encoding="utf-8")
    events = open(os.path.join(args.state, "events.jsonl"), "a", encoding="utf-8")
    thoughts = open(os.path.join(args.state, "thoughts.jsonl"), "a", encoding="utf-8")
    snapshots = open(os.path.join(args.state, "snapshots.jsonl"), "a", encoding="utf-8")
    os.makedirs(os.path.join(args.state, "agents"), exist_ok=True)
    unparsed = 0

    def save():
        for f in (chron, events, thoughts, snapshots):
            f.flush()
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(world.to_dict(), fh, indent=1)
        os.replace(tmp, sp)

    state = {"wrote_header": False}

    def header():
        if not state["wrote_header"]:
            chron.write(f"\n### Day {world.day}, {world.hour:02d}:00 - {world.season}, {world.weather()}\n")
            state["wrote_header"] = True

    def take_turn(a: Agent) -> bool:
        """One call to this resident's mind."""
        nonlocal unparsed
        aid = a.id
        obs = world.observe(a, args.recall)
        act, k = None, 0
        while act is None:
            try:
                act = brains[aid].decide(aid, obs)
            except Exception as e:
                if isinstance(brains[aid], ScriptedBrain):
                    act = {"action": "wait"}
                    continue
                # quota used up, network down, etc: pause the world and try the same turn again
                w = min(args.retry_wait * 2 ** k, 1800)
                k += 1
                print(f"[hour {world.tick}] {e}. World paused; retrying in {w:.0f}s "
                      f"(Ctrl+C to stop, run again later to resume).", file=sys.stderr)
                time.sleep(w)
        world.calls += 1
        world.day_calls += 1
        if act.pop("_unparsed", False):
            unparsed += 1
            print(f"[warn] {aid}: reply was not valid JSON ({unparsed} so far); treated as 'wait'", file=sys.stderr)
        thought = act.pop("thought", "")
        thoughts.write(json.dumps(dict(tick=world.tick, agent=aid, thought=thought)) + "\n")
        agent_dir = os.path.join(args.state, "agents", aid)
        os.makedirs(agent_dir, exist_ok=True)
        with open(os.path.join(agent_dir, "history.jsonl"), "a", encoding="utf-8") as agent_history:
            agent_history.write(json.dumps(dict(tick=world.tick, agent=aid, observation=obs,
                                               thought=thought, response=act), ensure_ascii=False) + "\n")
            agent_history.flush()
        for one in actions_of(act):
            if world.agents[aid] is not a:          # this resident died mid-reply
                break
            result = world.apply(a, one)
            header()
            chron.write(f"- **{world.label(a)}** @ {PLACES[a.location]['name']}: {result}\n")
            print(f"[d{world.day} {world.hour:02d}h call#{world.calls}] {aid}: {result[:110]}", file=sys.stderr)
        if world.agents[aid] is a:
            a.wake, a.idle = False, 0
        return True

    def flush_events():
        for ev in world.log:
            if ev["action"].get("action") in ("_death", "_arrive") and ev["tick"] >= START_TICK and not fresh_only(ev):
                header()
                chron.write(f"- *{ev['result']}*\n")
            events.write(json.dumps(ev, ensure_ascii=False) + "\n")
            if ev["action"].get("action") == "_snap" and ev.get("extra", {}).get("snap"):
                snapshots.write(json.dumps({"tick": ev["tick"], **ev["extra"]["snap"]}, ensure_ascii=False) + "\n")
        world.log = []

    def fresh_only(ev):                              # founders' arrival lines are not chronicle news
        return ev["action"].get("action") == "_arrive" and ev["result"].startswith("A founding")

    try:
        for _ in (itertools.count() if (args.forever or args.ticks is None) else range(args.ticks)):
            if args.max_calls and world.calls >= args.max_calls:
                print(f"Call budget reached ({world.calls} calls). State saved; run again to continue.",
                      file=sys.stderr)
                break
            state["wrote_header"] = False
            if world.calls_day != world.day:
                world.calls_day, world.day_calls = world.day, 0
            world.emit(None, {"action": "_tick"}, "")
            if world.hour == WAKE_HOUR and world.sleep_on:
                for a in world.agents.values():       # morning: everyone gets one turn
                    a.wake = True
            world.begin_hour()                        # wheat grows, hunger, night cold, deaths and arrivals

            order = list(AGENT_IDS)
            random.Random(args.seed * 1000003 + world.tick).shuffle(order)
            for aid in order:
                a = world.agents[aid]
                if should_act(world, a, args):
                    take_turn(a)
                else:
                    a.idle += 1
            world.emit(None, {"action": "_snap"}, "", {"snap": world.snapshot()})
            flush_events()
            world.tick += 1
            save()
            if args.delay:
                time.sleep(args.delay)
        print(f"Done. Now day {world.day}, hour {world.hour:02d}:00. {world.calls} model calls in total. "
              f"{len(world.dead)} deaths. Read {args.state}/chronicle.md")
        for b in cache.values():
            if isinstance(b, OpenAIBrain):
                print(f"  {b.provider} {','.join(b.models)}: {b.calls} calls, {b.tokens_in:,} tokens in, "
                      f"{b.tokens_out:,} out this run")
    except KeyboardInterrupt:
        print(f"\nStopped at day {world.day}, hour {world.hour:02d}:00 ({world.calls} calls). "
              f"State is saved; run the same command to resume.")
    finally:
        flush_events()
        for f in (chron, events, thoughts, snapshots):
            f.flush()
            f.close()


def main():
    ap = argparse.ArgumentParser(description="Agent Society v3: a 3D village of four minds.")
    ap.add_argument("--brain", choices=KINDS, default="nim",
                    help="brain for all residents (or use --lineup for one each)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--lineup", nargs="+", metavar="KIND[:MODEL]", default=None,
                    help="one brain per resident A B C D, e.g. --lineup nim:kimi nim:glm "
                         "(fewer than four entries are repeated)")
    ap.add_argument("--list-models", metavar="KIND", help="print the model ids available to your key, then exit")
    ap.add_argument("--check", action="store_true", help="make one real test call per resident, then exit")
    ap.add_argument("--ticks", type=int, default=None, help="stop after this many world hours (default: run forever)")
    ap.add_argument("--state", default=None,
                    help="output/resume folder; by default each new run gets a unique folder under runs/")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--forever", action="store_true", help="keep running until you press Ctrl+C")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds of real time between world hours")
    ap.add_argument("--retry-wait", type=float, default=60.0,
                    help="seconds to wait before retrying a failed turn (doubles each time, max 30 min)")
    # cost and pacing
    ap.add_argument("--idle-wake", type=int, default=4,
                    help="a quiet resident is still called after this many world hours (default 4)")
    ap.add_argument("--daily-calls", type=int, default=0,
                    help="max model calls per world day; the rest of the day passes quietly (0 = no cap)")
    ap.add_argument("--max-calls", type=int, default=0,
                    help="stop the run after about this many model calls in total (0 = no cap)")
    ap.add_argument("--recall", type=int, default=50, help="how many recent lines of experience each mind sees")
    ap.add_argument("--no-sleep", action="store_true", help="no night: minds may act at any hour")
    # society
    ap.add_argument("--lifespan-days", type=int, default=0,
                    help="residents also die of old age after N world days (0 = they only die of harm, default)")
    ap.add_argument("--temperaments", action="store_true",
                    help="give each resident a faint inner leaning (off by default: nobody is nudged)")
    # viewer
    ap.add_argument("--view-only", action="store_true", help="only run the viewer (watch or replay a state folder)")
    ap.add_argument("--no-viewer", action="store_true", help="run the world without starting the viewer")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to watch from a phone on the same Wi-Fi")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if args.state is None:
        args.state = ("state_village" if args.view_only else os.path.join(
            "runs", datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + f"_seed{args.seed}"))

    if args.list_models:
        list_models(args.list_models)
        return
    if args.check:
        check_lineup(args)
        return
    if args.view_only:
        if start_viewer(args.state, args.host, args.port, not args.no_browser):
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
        return
    if not args.no_viewer:
        start_viewer(args.state, args.host, args.port, not args.no_browser)
    run_world(args)


if __name__ == "__main__":
    main()