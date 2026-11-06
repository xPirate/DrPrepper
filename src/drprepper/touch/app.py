"""Touchscreen UI. Designed for 1024x600, gloved fingers and bright light:
large targets (>= 56 px), high contrast, one task per screen, an always-visible
Back/Home bar and a built-in on-screen keyboard."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, ObjectProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager

from ..core.db import LiveDrugDB
from ..core.text import chunk_text

COLORS = {
    "bg": (0.07, 0.09, 0.08, 1), "panel": (0.12, 0.15, 0.13, 1), "ink": (0.93, 0.95, 0.94, 1),
    "muted": (0.62, 0.68, 0.65, 1), "accent": (0.20, 0.62, 0.50, 1), "od": (0.78, 0.20, 0.12, 1),
    "warn": (0.95, 0.73, 0.25, 1), "key": (0.19, 0.23, 0.21, 1),
}
EHR_URL = os.environ.get("DRP_EHR_URL", "https://10.42.0.1:8443")

KV = """
#:import C drprepper.touch.app.COLORS

<Btn@Button>:
    background_normal: ''
    background_down: ''
    background_color: C['panel'] if self.state == 'normal' else C['accent']
    color: C['ink']
    font_size: '20sp'
    halign: 'center'
    valign: 'middle'
    text_size: self.width - dp(16), None
    markup: True

<OdBtn@Btn>:
    background_color: C['od'] if self.state == 'normal' else (0.95, 0.35, 0.25, 1)
    bold: True

<Txt@Label>:
    color: C['ink']
    font_size: '19sp'
    markup: True
    text_size: self.width, None
    size_hint_y: None
    height: self.texture_size[1] + dp(6)
    halign: 'left'
    valign: 'top'

<Heading@Txt>:
    font_size: '23sp'
    bold: True
    color: C['warn']
    height: self.texture_size[1] + dp(14)
    valign: 'bottom'

<ResultRow>:
    background_normal: ''
    background_down: ''
    background_color: (C['od'] if self.is_tox else C['panel']) if self.state == 'normal' else C['accent']
    color: C['ink']
    markup: True
    font_size: '19sp'
    halign: 'left'
    valign: 'middle'
    text_size: self.width - dp(24), self.height
    shorten: True
    shorten_from: 'right'
    padding: dp(12), 0

<TopBar>:
    size_hint_y: None
    height: dp(58)
    spacing: dp(6)
    padding: dp(6)
    canvas.before:
        Color:
            rgba: C['panel']
        Rectangle:
            pos: self.pos
            size: self.size
    Btn:
        text: '< Back'
        size_hint_x: None
        width: dp(120)
        on_release: app.go_back()
    Btn:
        text: 'Home'
        size_hint_x: None
        width: dp(110)
        on_release: app.go_home()
    Image:
        source: root.icon
        size_hint_x: None
        width: dp(46) if root.icon else 0
        opacity: 1 if root.icon else 0
        fit_mode: 'contain'
    Label:
        text: root.title
        font_size: '21sp'
        bold: True
        color: C['ink']
        text_size: self.size
        halign: 'left'
        valign: 'middle'
        shorten: True
        padding: dp(8), 0
    OdBtn:
        text: 'OVERDOSE'
        size_hint_x: None
        width: dp(150)
        on_release: app.open_tox('unknown-overdose')
    Label:
        text: root.clock
        font_size: '18sp'
        color: C['muted']
        size_hint_x: None
        width: dp(80)

<OSKeyboard>:
    orientation: 'vertical'
    size_hint_y: None
    height: dp(230)
    spacing: dp(4)
    padding: dp(4)
    canvas.before:
        Color:
            rgba: C['bg']
        Rectangle:
            pos: self.pos
            size: self.size

<Root>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: C['bg']
        Rectangle:
            pos: self.pos
            size: self.size
    TopBar:
        id: top
    Label:
        id: sample_bar
        text: root.sample_text
        markup: True
        size_hint_y: None
        height: (self.texture_size[1] + dp(10)) if root.sample_text else 0
        opacity: 1 if root.sample_text else 0
        text_size: self.width - dp(20), None
        halign: 'left'
        font_size: '16sp'
        color: (0.17, 0.11, 0, 1)
        canvas.before:
            Color:
                rgba: (0.94, 0.71, 0.16, 1) if root.sample_text else (0, 0, 0, 0)
            Rectangle:
                pos: self.pos
                size: self.size
    ScreenManager:
        id: sm
"""


class TopBar(BoxLayout):
    title = StringProperty("Dr.Prepper")
    icon = StringProperty("")
    clock = StringProperty("")


class Root(BoxLayout):
    sample_text = StringProperty("")


class ResultRow(Button):
    """RecycleView row. ref is ('drug', id) or ('tox', slug)."""
    is_tox = BooleanProperty(False)
    ref = ListProperty([])

    def on_release(self):
        App.get_running_app().open_ref(self.ref)


class OSKeyboard(BoxLayout):
    """Big-key QWERTY keyboard bound to one TextInput."""
    target = ObjectProperty(None, allownone=True)
    on_enter = ObjectProperty(None, allownone=True)

    ROWS = ["1234567890", "qwertyuiop", "asdfghjkl-", "zxcvbnm/.,"]

    def __init__(self, enter_label: str = "Search", **kw):
        super().__init__(**kw)
        for row in self.ROWS:
            g = GridLayout(cols=len(row), spacing=dp(4))
            for ch in row:
                g.add_widget(self._key(ch, ch))
            self.add_widget(g)
        bottom = BoxLayout(spacing=dp(4))
        bottom.add_widget(self._key("Clear", "\x00clear", size_hint_x=0.15))
        bottom.add_widget(self._key("Space", " ", size_hint_x=0.45))
        bottom.add_widget(self._key("< Del", "\x08", size_hint_x=0.15))
        bottom.add_widget(self._key(enter_label, "\n", size_hint_x=0.25, accent=True))
        self.add_widget(bottom)

    def _key(self, label, value, size_hint_x=1, accent=False):
        b = Button(text=label, font_size=sp(22), background_normal="", background_down="",
                   background_color=COLORS["accent"] if accent else COLORS["key"], color=COLORS["ink"],
                   size_hint_x=size_hint_x)
        b.bind(on_press=lambda *_: self.press(value))
        b.bind(state=lambda btn, st: setattr(btn, "background_color",
               COLORS["accent"] if (st == "down" or accent) else COLORS["key"]))
        return b

    def press(self, value: str) -> None:
        t = self.target
        if t is None:
            return
        if value == "\n":
            if self.on_enter:
                self.on_enter()
        elif value == "\x08":
            t.do_backspace()
        elif value == "\x00clear":
            t.text = ""
        else:
            t.insert_text(value)


# ------------------------------------------------------------------ screens
SCREENS_KV = """
#:import C drprepper.touch.app.COLORS

<HomeScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(14)
        spacing: dp(12)
        BoxLayout:
            size_hint_y: None
            height: dp(68)
            spacing: dp(8)
            Btn:
                text: '[size=22sp]Search drugs, brands, classes, street names...[/size]'
                color: C['muted']
                halign: 'left'
                on_release: app.open_search('')
            Button:
                text: '[b]INCOMING PATIENT[/b]\\nSend to triage'
                markup: True
                size_hint_x: None
                width: dp(260)
                font_size: '18sp'
                halign: 'center'
                background_normal: ''
                background_color: (0.95, 0.73, 0.25, 1) if self.state == 'normal' else (1, 0.85, 0.4, 1)
                color: (0.12, 0.08, 0, 1)
                on_release: app.open_intake('medical')
        GridLayout:
            cols: 3
            spacing: dp(12)
            OdBtn:
                text: '[size=26sp]UNKNOWN OVERDOSE[/size]\\nStart here'
                on_release: app.open_tox('unknown-overdose')
            OdBtn:
                text: '[size=26sp]Overdose cards[/size]\\nAll toxicology topics'
                on_release: app.open_tox_index()
            Btn:
                text: '[size=26sp]Toxidromes[/size]\\nPattern recognition'
                on_release: app.open_tox('toxidromes')
            Btn:
                text: '[size=26sp]Browse A-Z[/size]\\nAll medications'
                on_release: app.open_browse()
            Btn:
                text: '[size=26sp]Dose math[/size]\\nmg/kg, volume, lb/kg'
                on_release: app.show('calc')
            Btn:
                text: '[size=26sp]About / EHR[/size]\\nData date, web console'
                on_release: app.show('about')

<SearchScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(8), dp(6)
        spacing: dp(6)
        BoxLayout:
            size_hint_y: None
            height: dp(56)
            spacing: dp(6)
            TextInput:
                id: q
                multiline: False
                font_size: '24sp'
                hint_text: 'Type a drug, brand or street name'
                background_color: C['panel']
                foreground_color: C['ink']
                hint_text_color: C['muted']
                cursor_color: C['warn']
                padding: dp(12), dp(12)
                on_text: root.schedule_search()
                on_text_validate: root.run_search()
                on_focus: root.keyboard_visible = True if self.focus else root.keyboard_visible
            ToggleButton:
                id: f_all
                text: 'All'
                group: 'kind'
                state: 'down'
                size_hint_x: None
                width: dp(76)
                font_size: '19sp'
                allow_no_selection: False
                on_press: root.kind = ''; root.run_search()
            ToggleButton:
                text: 'Rx'
                group: 'kind'
                size_hint_x: None
                width: dp(70)
                font_size: '19sp'
                allow_no_selection: False
                on_press: root.kind = 'RX'; root.run_search()
            ToggleButton:
                text: 'OTC'
                group: 'kind'
                size_hint_x: None
                width: dp(76)
                font_size: '19sp'
                allow_no_selection: False
                on_press: root.kind = 'OTC'; root.run_search()
            Btn:
                text: 'Keyboard' if not root.keyboard_visible else 'Hide kbd'
                size_hint_x: None
                width: dp(118)
                font_size: '18sp'
                on_release: root.keyboard_visible = not root.keyboard_visible
        Label:
            text: root.status
            color: C['muted']
            font_size: '16sp'
            size_hint_y: None
            height: dp(22) if self.text else 0
            text_size: self.size
            halign: 'left'
        RecycleView:
            id: rv
            viewclass: 'ResultRow'
            bar_width: dp(10)
            scroll_type: ['bars', 'content']
            on_scroll_start: root.keyboard_visible = False
            RecycleBoxLayout:
                default_size: None, dp(60)
                default_size_hint: 1, None
                size_hint_y: None
                height: self.minimum_height
                orientation: 'vertical'
                spacing: dp(4)
        BoxLayout:
            id: kbd_holder
            size_hint_y: None
            height: self.minimum_height

<ListScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(8)
        spacing: dp(6)
        GridLayout:
            id: letters
            cols: 13
            size_hint_y: None
            height: dp(116) if root.show_letters else 0
            opacity: 1 if root.show_letters else 0
            spacing: dp(4)
        RecycleView:
            id: rv
            viewclass: 'ResultRow'
            bar_width: dp(10)
            scroll_type: ['bars', 'content']
            RecycleBoxLayout:
                default_size: None, dp(60)
                default_size_hint: 1, None
                size_hint_y: None
                height: self.minimum_height
                orientation: 'vertical'
                spacing: dp(4)

<DetailScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(8)
        spacing: dp(6)
        ScrollView:
            size_hint_y: None
            height: dp(58) if root.has_tabs else 0
            opacity: 1 if root.has_tabs else 0
            do_scroll_y: False
            bar_width: dp(4)
            BoxLayout:
                id: tabs
                size_hint_x: None
                width: self.minimum_width
                spacing: dp(6)
        ScrollView:
            id: scroll
            bar_width: dp(12)
            scroll_type: ['bars', 'content']
            BoxLayout:
                id: body
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10), dp(4), dp(18), dp(20)
                spacing: dp(4)

<CalcScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(10)
        spacing: dp(8)
        GridLayout:
            cols: 4
            size_hint_y: None
            height: dp(180)
            spacing: dp(8)
            Txt:
                text: 'Weight'
                size_hint_y: 1
                valign: 'middle'
            TextInput:
                id: wt
                multiline: False
                font_size: '24sp'
                input_filter: 'float'
                on_focus: if self.focus: root.target(self)
                on_text: root.recalc()
            ToggleButton:
                id: unit_kg
                text: 'kg'
                group: 'wunit'
                state: 'down'
                font_size: '20sp'
                allow_no_selection: False
                on_press: root.recalc()
            ToggleButton:
                text: 'lb'
                group: 'wunit'
                font_size: '20sp'
                allow_no_selection: False
                on_press: root.recalc()
            Txt:
                text: 'Dose (mg/kg)'
                size_hint_y: 1
                valign: 'middle'
            TextInput:
                id: mgkg
                multiline: False
                font_size: '24sp'
                input_filter: 'float'
                on_focus: if self.focus: root.target(self)
                on_text: root.recalc()
            Txt:
                text: 'Conc. (mg/mL)'
                size_hint_y: 1
                valign: 'middle'
            TextInput:
                id: conc
                multiline: False
                font_size: '24sp'
                input_filter: 'float'
                on_focus: if self.focus: root.target(self)
                on_text: root.recalc()
            Txt:
                text: 'Max dose (mg)'
                size_hint_y: 1
                valign: 'middle'
            TextInput:
                id: maxd
                multiline: False
                font_size: '24sp'
                input_filter: 'float'
                on_focus: if self.focus: root.target(self)
                on_text: root.recalc()
            Label:
            Label:
        Label:
            text: root.result
            markup: True
            font_size: '24sp'
            color: C['ink']
            text_size: self.width, None
            halign: 'left'
        GridLayout:
            id: pad
            cols: 4
            size_hint_y: None
            height: dp(190)
            spacing: dp(4)

<AboutScreen>:
    ScrollView:
        BoxLayout:
            id: body
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(6)
"""


class HomeScreen(Screen):
    pass


class SearchScreen(Screen):
    kind = StringProperty("")
    status = StringProperty("")
    keyboard_visible = BooleanProperty(True)

    def __init__(self, **kw):
        super().__init__(**kw)
        self._ev = None
        self.kbd = OSKeyboard()
        Clock.schedule_once(self._setup)

    def _setup(self, *_):
        self.kbd.target = self.ids.q
        self.kbd.on_enter = self._enter
        self.on_keyboard_visible(self, self.keyboard_visible)

    def _enter(self):
        self.run_search()
        self.keyboard_visible = False

    def on_keyboard_visible(self, _w, visible):
        holder = self.ids.get("kbd_holder")
        if holder is None:
            return
        holder.clear_widgets()
        if visible:
            holder.add_widget(self.kbd)

    def schedule_search(self):
        if self._ev:
            self._ev.cancel()
        self._ev = Clock.schedule_once(lambda *_: self.run_search(), 0.25)

    def run_search(self):
        app = App.get_running_app()
        q = self.ids.q.text.strip()
        if not q:
            self.ids.rv.data = []
            self.status = ""
            return
        res = app.db.search(q, limit=80, kind=self.kind or None)
        rows = [dict(text=f"[b]OVERDOSE:[/b] {t.name}  [size=16sp]{t.summary}[/size]", is_tox=True, ref=["tox", t.slug])
                for t in res.tox]
        rows += [dict(text=drug_row_text(d), is_tox=False, ref=["drug", d.id]) for d in res.drugs]
        self.ids.rv.data = rows
        self.ids.rv.scroll_y = 1
        self.status = f"{len(res.tox)} overdose card(s), {len(res.drugs)} medication(s)" if rows else "No matches. Try fewer letters or a generic name."


def drug_row_text(d) -> str:
    klass = d.pharm_class.split(" | ")[0] if d.pharm_class else ""
    return (f"[b]{d.display}[/b]  [color=9aa49e][size=16sp]{d.route} · {d.kind}"
            f"{' · ' + klass if klass else ''}[/size][/color]")


class ListScreen(Screen):
    show_letters = BooleanProperty(False)

    def __init__(self, **kw):
        super().__init__(**kw)
        Clock.schedule_once(self._letters)

    def _letters(self, *_):
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            b = Button(text=ch, font_size=sp(22), background_normal="", background_color=COLORS["key"], color=COLORS["ink"])
            b.bind(on_release=lambda btn: self.load_letter(btn.text))
            self.ids.letters.add_widget(b)

    def load_letter(self, ch: str):
        app = App.get_running_app()
        rows = [dict(text=drug_row_text(d), is_tox=False, ref=["drug", d.id]) for d in app.db.browse(ch)]
        if not rows:
            rows = [dict(text=f"No medications under {ch}", is_tox=False, ref=[])]
        self.ids.rv.data = rows
        self.ids.rv.scroll_y = 1
        app.set_title(f"Browse: {ch} ({len(rows)})")

    def load_tox(self):
        app = App.get_running_app()
        rows, last = [], None
        for t in app.db.list_tox():
            if t.category != last:
                rows.append(dict(text=f"[color=f2c35b][b]{t.category.upper()}[/b][/color]", is_tox=False, ref=[]))
                last = t.category
            rows.append(dict(text=f"[b]{t.name}[/b]  [size=16sp]{t.summary}[/size]", is_tox=True, ref=["tox", t.slug]))
        self.ids.rv.data = rows
        self.ids.rv.scroll_y = 1


class DetailScreen(Screen):
    has_tabs = BooleanProperty(False)
    sections = ListProperty([])
    current = NumericProperty(0)

    def show_drug(self, d: dict):
        body = self.ids.body
        body.clear_widgets()
        self.has_tabs = True
        tabs = self.ids.tabs
        tabs.clear_widgets()
        overview = {"title": "Overview", "drug": d}
        self.sections = [overview] + d["sections"]
        for i, sct in enumerate(self.sections):
            b = Button(text=sct["title"], font_size=sp(18), size_hint_x=None, background_normal="",
                       background_color=COLORS["od"] if sct.get("key") == "overdosage" else COLORS["key"],
                       color=COLORS["ink"])
            b.texture_update()
            b.width = max(dp(110), b.texture_size[0] + dp(28))
            b.bind(on_release=lambda _b, i=i: self.select(i))
            tabs.add_widget(b)
        self.select(0)

    def select(self, i: int):
        self.current = i
        for j, b in enumerate(reversed(self.ids.tabs.children)):
            sct = self.sections[j]
            base = COLORS["od"] if sct.get("key") == "overdosage" else COLORS["key"]
            b.background_color = COLORS["accent"] if j == i else base
        body = self.ids.body
        body.clear_widgets()
        sct = self.sections[i]
        if "drug" in sct:
            self._overview(sct["drug"])
        else:
            body.add_widget(_heading(sct["title"]))
            for para in chunk_text(sct["body"]):
                body.add_widget(_txt(_esc(para)))
        self.ids.scroll.scroll_y = 1

    def _overview(self, d: dict):
        body = self.ids.body
        body.add_widget(_heading(f"{d['display']}  ({d['kind']})", size=26))
        body.add_widget(_txt(f"[b]Route:[/b] {_esc(d['route'] or '?')}"))
        if d.get("brands"):
            body.add_widget(_txt(f"[b]Brands:[/b] {_esc(d['brands'])}"))
        if d.get("pharm_class"):
            body.add_widget(_txt(f"[b]Class:[/b] {_esc(d['pharm_class'])}"))
        for t in d.get("tox", []):
            b = Button(text=f"[b]OVERDOSE CARD:[/b] {t['name']}", markup=True, font_size=sp(20), size_hint_y=None,
                       height=dp(60), background_normal="", background_color=COLORS["od"], color=COLORS["ink"])
            b.bind(on_release=lambda _b, slug=t["slug"]: App.get_running_app().open_tox(slug))
            body.add_widget(b)
        keys = [s.get("key") for s in self.sections]
        quick = [k for k in ("boxed_warning", "dosage_and_administration", "directions", "contraindications",
                             "do_not_use", "overdosage") if k in keys]
        if quick:
            body.add_widget(_heading("Jump to"))
            row = GridLayout(cols=3, size_hint_y=None, spacing=dp(6))
            row.bind(minimum_height=row.setter("height"))
            for k in quick:
                idx = keys.index(k)
                b = Button(text=self.sections[idx]["title"], font_size=sp(19), size_hint_y=None, height=dp(58),
                           background_normal="", color=COLORS["ink"],
                           background_color=COLORS["od"] if k in ("overdosage", "boxed_warning") else COLORS["key"])
                b.bind(on_release=lambda _b, i=idx: self.select(i))
                row.add_widget(b)
            body.add_widget(row)
        body.add_widget(_txt(f"[size=15sp][color=9aa49e]FDA label effective {d.get('effective') or '?'}; "
                             f"{d.get('label_count') or 1} manufacturer label(s) merged.[/color][/size]"))

    def show_tox(self, t: dict, disclaimer: str):
        self.has_tabs = False
        self.ids.tabs.clear_widgets()
        body = self.ids.body
        body.clear_widgets()
        body.add_widget(_heading(t["name"], size=26))
        if t.get("slug") == "unknown-overdose":
            b = Button(text="[b]SEND INCOMING PATIENT TO TRIAGE[/b]   age, sex, ETA from the radio report", markup=True,
                       font_size=sp(19), size_hint_y=None, height=dp(64), background_normal="",
                       background_color=(0.95, 0.73, 0.25, 1), color=(0.12, 0.08, 0, 1))
            b.bind(on_release=lambda *_: App.get_running_app().open_intake("overdose"))
            body.add_widget(b)
        if t.get("review_status") == "draft":
            body.add_widget(_txt("[color=f2c35b][b]DRAFT[/b] - not yet reviewed by a medical director.[/color]"))
        body.add_widget(_txt(f"[b]{_esc(t.get('summary') or '')}[/b]"))
        for sct in t["sections"]:
            avoid = sct["title"].lower() == "avoid"
            body.add_widget(_heading(sct["title"], color=COLORS["od"] if avoid else None))
            for item in sct["items"]:
                body.add_widget(_txt(f"•  {_esc(item)}"))
        if disclaimer:
            body.add_widget(_txt(f"\n[size=15sp][color=9aa49e]{_esc(disclaimer)}[/color][/size]"))
        self.ids.scroll.scroll_y = 1


class IntakeScreen(Screen):
    """Incoming-patient report for the triage board, built for gloved taps: one tap per answer."""

    GROUPS = [
        ("age_band", "Age", [("infant", "Infant <1"), ("child", "Child 1-12"), ("teen", "Teen 13-17"),
                             ("adult", "Adult"), ("older", "Older 65+"), ("unknown", "Unknown")]),
        ("sex", "Sex", [("male", "Male"), ("female", "Female"), ("unknown", "Unknown")]),
        ("concern", "Concern", [("overdose", "Overdose ?"), ("opioid", "Opioid"), ("stimulant", "Stimulant"),
                                ("alcohol", "Alcohol"), ("trauma", "Trauma"), ("medical", "Medical"), ("other", "Other")]),
        ("avpu", "Responds", [("alert", "Alert"), ("voice", "To voice"), ("pain", "To pain"),
                              ("unresponsive", "Unresponsive"), ("unknown", "Unknown")]),
        ("breathing", "Breathing", [("normal", "Normal"), ("slow", "Slow / shallow"), ("none", "Not breathing"),
                                    ("unknown", "Unknown")]),
        ("naloxone", "Naloxone given", [("no", "No"), ("yes", "Yes"), ("unknown", "Unknown")]),
        ("eta_min", "ETA", [("0", "Here now"), ("5", "5 min"), ("10", "10 min"), ("15", "15 min"), ("30", "30 min"),
                            ("60", "1 hour"), ("", "Unknown")]),
    ]
    FIELDS = [("age", "Approx. age (optional)", "numeric"), ("location", "Location / unit / found at", "text"),
              ("notes", "Notes from the report", "text"), ("reporter", "Your name or call sign", "text")]
    status = StringProperty("")
    sending = BooleanProperty(False)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.values: dict[str, str] = {}
        self.buttons: dict[str, list] = {}
        self.inputs: dict = {}
        self.pin_required = False
        self.kbd = OSKeyboard(enter_label="Done")
        self.kbd.on_enter = self._hide_kbd
        self._build()

    # ---------------- layout
    def _build(self):
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.textinput import TextInput
        root = BoxLayout(orientation="vertical")
        self.scroll = ScrollView(bar_width=dp(10), scroll_type=["bars", "content"])
        form = GridLayout(cols=1, size_hint_y=None, spacing=dp(6), padding=(dp(10), dp(8)))
        form.bind(minimum_height=form.setter("height"))
        for key, label, opts in self.GROUPS:
            row = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(5))
            row.add_widget(Label(text=label, size_hint_x=None, width=dp(128), font_size=sp(17), bold=True,
                                 color=COLORS["warn"], halign="left", valign="middle", text_size=(dp(128), None)))
            self.buttons[key] = []
            for val, txt in opts:
                b = Button(text=txt, font_size=sp(17), background_normal="", background_down="",
                           background_color=COLORS["key"], color=COLORS["ink"])
                b.bind(on_release=lambda _b, k=key, v=val: self.pick(k, v))
                b._val = val
                self.buttons[key].append(b)
                row.add_widget(b)
            form.add_widget(row)
        for key, label, kind in self.FIELDS:
            row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
            row.add_widget(Label(text=label, size_hint_x=None, width=dp(260), font_size=sp(17), color=COLORS["ink"],
                                 halign="left", valign="middle", text_size=(dp(260), None)))
            ti = TextInput(multiline=False, font_size=sp(20), background_color=COLORS["panel"], foreground_color=COLORS["ink"],
                           cursor_color=COLORS["warn"], padding=(dp(10), dp(12)), write_tab=False,
                           input_filter="int" if kind == "numeric" else None)
            ti.bind(focus=lambda w, f: self._focus(w, f))
            self.inputs[key] = ti
            row.add_widget(ti)
            form.add_widget(row)
        prow = BoxLayout(size_hint_y=None, height=0, opacity=0, spacing=dp(6))
        prow.add_widget(Label(text="Intake PIN", size_hint_x=None, width=dp(260), font_size=sp(17), bold=True,
                              color=COLORS["warn"], halign="left", valign="middle", text_size=(dp(260), None)))
        self.pin_input = TextInput(multiline=False, password=True, input_filter="int", font_size=sp(20),
                                   background_color=COLORS["panel"], foreground_color=COLORS["ink"], padding=(dp(10), dp(12)))
        self.pin_input.bind(focus=lambda w, f: self._focus(w, f))
        prow.add_widget(self.pin_input)
        self.pin_row = prow
        form.add_widget(prow)
        self.scroll.add_widget(form)
        root.add_widget(self.scroll)

        self.kbd_holder = BoxLayout(size_hint_y=None, height=0)
        root.add_widget(self.kbd_holder)

        bar = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(8), padding=(dp(8), dp(6)))
        cancel = Button(text="Cancel", size_hint_x=None, width=dp(150), font_size=sp(19), background_normal="",
                        background_color=COLORS["key"], color=COLORS["ink"])
        cancel.bind(on_release=lambda *_: App.get_running_app().go_back())
        self.status_lbl = Label(text="", markup=True, font_size=sp(16), color=COLORS["ink"], halign="left", valign="middle")
        self.status_lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.bind(status=lambda _w, v: setattr(self.status_lbl, "text", v))
        self.send_btn = Button(text="[b]SEND TO TRIAGE[/b]", markup=True, size_hint_x=None, width=dp(290), font_size=sp(21),
                               background_normal="", background_color=COLORS["od"], color=COLORS["ink"])
        self.send_btn.bind(on_release=lambda *_: self.send())
        bar.add_widget(cancel); bar.add_widget(self.status_lbl); bar.add_widget(self.send_btn)
        root.add_widget(bar)
        self.add_widget(root)

    def pick(self, key: str, val: str):
        self.values[key] = val
        for b in self.buttons[key]:
            b.background_color = COLORS["accent"] if b._val == val else COLORS["key"]

    def _focus(self, w, focused):
        if focused:
            self.kbd.target = w
            if not self.kbd.parent:
                self.kbd_holder.add_widget(self.kbd)
                self.kbd_holder.height = self.kbd.height
            Clock.schedule_once(lambda *_: self.scroll.scroll_to(w.parent, padding=dp(10)), 0.05)

    def _hide_kbd(self):
        if self.kbd.target:
            self.kbd.target.focus = False
        self.kbd_holder.clear_widgets()
        self.kbd_holder.height = 0

    # ---------------- lifecycle
    def reset(self, concern: str):
        self.values = {}
        defaults = {"age_band": "unknown", "sex": "unknown", "concern": concern, "avpu": "unknown",
                    "breathing": "unknown", "naloxone": "unknown", "eta_min": ""}
        for k, v in defaults.items():
            self.pick(k, v)
        for ti in self.inputs.values():
            ti.text = ""
        self.pin_input.text = ""
        self._hide_kbd()
        self.scroll.scroll_y = 1
        self.sending = False
        self.send_btn.disabled = False
        self.status = "Tap what the radio report says. Anything you skip is sent as unknown."
        self._load_config()

    def _load_config(self):
        import threading
        from . import intake_client

        def work():
            try:
                cfg = intake_client.get_config()
                msg = None if cfg.get("enabled", True) else "[color=ff8a73]Intake is turned off on the server. Use the radio or a runner.[/color]"
                Clock.schedule_once(lambda *_: self._apply_config(cfg.get("pin_required", False), msg))
            except intake_client.IntakeError as e:
                err = str(e)
                Clock.schedule_once(lambda *_: self._apply_config(False, f"[color=ff8a73]{_esc(err)}[/color]"))
        threading.Thread(target=work, daemon=True).start()

    def _apply_config(self, pin_required: bool, msg: str | None):
        self.pin_required = pin_required
        self.pin_row.height = dp(52) if pin_required else 0
        self.pin_row.opacity = 1 if pin_required else 0
        if msg:
            self.status = msg

    def report(self) -> dict:
        r = {k: self.values.get(k, "unknown") for k in ("age_band", "sex", "concern", "avpu", "breathing", "naloxone")}
        eta = self.values.get("eta_min", "")
        r["eta_min"] = int(eta) if eta else None
        for k in ("age", "location", "notes", "reporter"):
            r[k] = self.inputs[k].text.strip()
        if self.pin_required:
            r["pin"] = self.pin_input.text.strip()
        return r

    def send(self):
        import threading
        from . import intake_client
        if self.sending:
            return
        self._hide_kbd()
        self.sending = True
        self.send_btn.disabled = True
        self.status = "Sending to triage…"
        payload = self.report()

        def work():
            try:
                res = intake_client.send_report(payload)
                Clock.schedule_once(lambda *_: self._sent(res))
            except intake_client.IntakeError as e:
                err = str(e)
                Clock.schedule_once(lambda *_: self._failed(err))
        threading.Thread(target=work, daemon=True).start()

    def _sent(self, res: dict):
        self.sending = False
        App.get_running_app().intake_sent(res.get("pnum", "?"), self.report())

    def _failed(self, err: str):
        self.sending = False
        self.send_btn.disabled = False
        self.status = f"[color=ff8a73][b]NOT SENT.[/b] {_esc(err)}[/color]  Tap Send to try again."


class IntakeDoneScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(24), spacing=dp(16))
        self.msg = Label(text="", markup=True, font_size=sp(24), color=COLORS["ink"], halign="center", valign="middle")
        self.msg.bind(size=lambda w, s: setattr(w, "text_size", (s[0], None)))
        box.add_widget(self.msg)
        row = BoxLayout(size_hint_y=None, height=dp(72), spacing=dp(12))
        again = Button(text="Report another patient", font_size=sp(20), background_normal="", background_color=COLORS["key"],
                       color=COLORS["ink"])
        again.bind(on_release=lambda *_: App.get_running_app().open_intake("overdose"))
        card = Button(text="[b]Unknown overdose steps[/b]", markup=True, font_size=sp(20), background_normal="",
                      background_color=COLORS["od"], color=COLORS["ink"])
        card.bind(on_release=lambda *_: App.get_running_app().open_tox("unknown-overdose"))
        home = Button(text="Home", font_size=sp(20), background_normal="", background_color=COLORS["accent"], color=COLORS["ink"])
        home.bind(on_release=lambda *_: App.get_running_app().go_home())
        row.add_widget(again); row.add_widget(card); row.add_widget(home)
        box.add_widget(row)
        self.add_widget(box)

    def show(self, pnum: str, r: dict):
        eta = r.get("eta_min")
        eta_txt = "arriving now" if eta == 0 else (f"ETA {eta} min" if eta else "ETA unknown")
        self.msg.text = (f"[size=30sp][b][color=7fd49a]SENT TO TRIAGE[/color][/b][/size]\n\n"
                         f"Patient number [b]{pnum}[/b] · {eta_txt}\n\n"
                         f"[size=18sp]Triage sees this on the triage board now. Give them the patient number on arrival.[/size]")


class CalcScreen(Screen):
    """Arithmetic helper only: weight x mg/kg (capped by max) and volume from concentration."""
    result = StringProperty("Enter weight and dose.")

    def __init__(self, **kw):
        super().__init__(**kw)
        self._target = None
        Clock.schedule_once(self._pad)

    def _pad(self, *_):
        for k in ["7", "8", "9", "< Del", "4", "5", "6", "Clear", "1", "2", "3", "Next", "0", ".", "", ""]:
            b = Button(text=k, font_size=sp(24), background_normal="", background_color=COLORS["key"], color=COLORS["ink"],
                       disabled=not k)
            b.bind(on_release=lambda btn: self._key(btn.text))
            self.ids.pad.add_widget(b)
        self.target(self.ids.wt)

    def target(self, ti):
        self._target = ti

    def _key(self, k: str):
        t = self._target
        if not t:
            return
        order = [self.ids.wt, self.ids.mgkg, self.ids.conc, self.ids.maxd]
        if k == "< Del":
            t.text = t.text[:-1]
        elif k == "Clear":
            t.text = ""
        elif k == "Next":
            nxt = order[(order.index(t) + 1) % len(order)]
            nxt.focus = True
            self._target = nxt
        elif k == "." and "." in t.text:
            return
        else:
            t.text += k

    def recalc(self):
        def f(ti):
            try:
                return float(ti.text)
            except ValueError:
                return None
        w, mgkg, conc, mx = f(self.ids.wt), f(self.ids.mgkg), f(self.ids.conc), f(self.ids.maxd)
        if w is None:
            self.result = "Enter weight."
            return
        kg = w if self.ids.unit_kg.state == "down" else w * 0.45359237
        lines = [f"Weight: [b]{kg:.1f} kg[/b]  ({kg / 0.45359237:.1f} lb)"]
        if mgkg is not None:
            dose = kg * mgkg
            capped = mx is not None and dose > mx
            if capped:
                dose = mx
            lines.append(f"Dose: [b]{dose:.2f} mg[/b]" + ("  [color=f2c35b](capped at max)[/color]" if capped else ""))
            if conc:
                lines.append(f"Volume: [b]{dose / conc:.2f} mL[/b] at {conc:g} mg/mL")
        lines.append("[size=15sp][color=9aa49e]Arithmetic only. Verify the dose, concentration and maximum against the label or card.[/color][/size]")
        self.result = "\n".join(lines)


class AboutScreen(Screen):
    def fill(self, meta: dict):
        body = self.ids.body
        body.clear_widgets()
        body.add_widget(_heading(App.get_running_app().brand_title, size=26))
        body.add_widget(_txt(f"[b]EHR web console:[/b] join this unit's Wi-Fi, then open [b]{EHR_URL}[/b]"))
        body.add_widget(_txt(f"[b]Drug data built:[/b] {_esc(meta.get('built_at', '?'))}"))
        body.add_widget(_txt(f"[b]Sources:[/b] {_esc(meta.get('sources', '?'))}"))
        body.add_widget(_txt(f"[b]Disclaimer:[/b] {_esc(meta.get('disclaimer', ''))}"))
        import json
        refs = json.loads(meta.get("references") or "[]")
        if refs:
            body.add_widget(_heading("Card references"))
            for r in refs:
                body.add_widget(_txt(f"•  {_esc(r)}"))
        has_pin = exit_pin_hash() is not None
        b = Button(text="Exit to desktop (admin PIN)" if has_pin else "Exit to desktop: set an exit PIN in Admin › Touchscreen",
                   size_hint_y=None, height=dp(56), font_size=sp(18), disabled=not has_pin,
                   background_normal="", background_color=COLORS["key"], color=COLORS["ink"])
        b.bind(on_release=lambda *_: ExitPinPad().open())
        body.add_widget(b)


# ------------------------------------------------------------------ exit PIN
# Leaving the kiosk needs the PIN set in Admin > Touchscreen. Only its salted hash is in the kiosk folder.
_exit_fails: list[float] = []


def exit_pin_hash() -> str | None:
    from .intake_client import kiosk_dir
    try:
        h = (kiosk_dir() / "exit-pin").read_text().strip()
        return h or None
    except OSError:
        return None


class ExitPinPad(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", spacing=dp(6), padding=dp(10), **kw)
        from kivy.uix.popup import Popup
        self.digits = ""
        self.msg = Label(text="Admin PIN to leave the kiosk", font_size=sp(20), color=COLORS["ink"],
                         size_hint_y=None, height=dp(40))
        self.add_widget(self.msg)
        self.dots = Label(text="", font_size=sp(32), color=COLORS["warn"], size_hint_y=None, height=dp(44))
        self.add_widget(self.dots)
        grid = GridLayout(cols=3, spacing=dp(6))
        for k in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "Clear", "0", "OK"]:
            btn = Button(text=k, font_size=sp(24), background_normal="", background_color=COLORS["key"], color=COLORS["ink"])
            btn.bind(on_release=lambda w, k=k: self.press(k))
            grid.add_widget(btn)
        self.add_widget(grid)
        cancel = Button(text="Cancel", size_hint_y=None, height=dp(52), font_size=sp(19), background_normal="",
                        background_color=COLORS["panel"], color=COLORS["ink"])
        cancel.bind(on_release=lambda *_: self.popup.dismiss())
        self.add_widget(cancel)
        self.popup = Popup(title="", separator_height=0, content=self, size_hint=(None, None), size=(dp(420), dp(520)),
                           auto_dismiss=False)

    def open(self):
        self.popup.open()

    def press(self, k: str):
        global _exit_fails
        now = time.monotonic()
        _exit_fails = [t for t in _exit_fails if now - t < 300]
        if len(_exit_fails) >= 5:
            self.msg.text = "Too many wrong PINs. Wait 5 minutes."
            return
        if k == "Clear":
            self.digits = ""
        elif k == "OK":
            from ..server.auth import verify_password
            h = exit_pin_hash()
            if h and verify_password(self.digits, h):
                self.popup.dismiss()
                App.get_running_app().stop()
                return
            _exit_fails.append(now)
            self.digits = ""
            self.msg.text = "Wrong PIN."
        elif len(self.digits) < 8:
            self.digits += k
        self.dots.text = "•" * len(self.digits)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("[", "&bl;").replace("]", "&br;")


def _txt(text: str) -> Label:
    lbl = Label(text=text, markup=True, font_size=sp(19), color=COLORS["ink"], size_hint_y=None,
                halign="left", valign="top")
    lbl.bind(width=lambda l, w: setattr(l, "text_size", (w, None)),
             texture_size=lambda l, ts: setattr(l, "height", ts[1] + dp(6)))
    return lbl


def _heading(text: str, size: int = 22, color=None) -> Label:
    lbl = _txt(f"[b]{_esc(text)}[/b]")
    lbl.font_size = sp(size)
    lbl.color = color or COLORS["warn"]
    lbl.padding = (0, dp(8))
    return lbl


# ------------------------------------------------------------------ app
class DrPrepperApp(App):
    title = "Dr.Prepper"

    def __init__(self, db_path: str | None = None, screenshot_dir: str | None = None, **kw):
        super().__init__(**kw)
        self.db = LiveDrugDB(db_path)
        if not self.db:
            raise FileNotFoundError(f"drug database not found: {self.db.path}")
        self.meta = self.db.meta()
        self._gen = self.db.generation
        self.history: list[tuple[str, str, object]] = []
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None

    def build(self):
        Window.clearcolor = COLORS["bg"]
        Builder.load_string(KV)
        Builder.load_string(SCREENS_KV)
        root = Root()
        self.sm: ScreenManager = root.ids.sm
        self.sm.transition = NoTransition()
        for cls, name in ((HomeScreen, "home"), (SearchScreen, "search"), (ListScreen, "list"),
                          (DetailScreen, "detail"), (CalcScreen, "calc"), (AboutScreen, "about"),
                          (IntakeScreen, "intake"), (IntakeDoneScreen, "intake_done")):
            self.sm.add_widget(cls(name=name))
        self.top = root.ids.top
        self.root_widget = root
        self._tick()
        Clock.schedule_interval(self._tick, 20)
        self._check_data()
        Clock.schedule_interval(self._check_data, 10)
        Window.bind(on_keyboard=self._hw_key)
        if self.screenshot_dir:
            Clock.schedule_once(self._screenshots, 1)
        return root

    # ---- navigation
    def _tick(self, *_):
        self.top.clock = datetime.now().strftime("%H:%M")

    @property
    def home_title(self) -> str:
        custom = (getattr(self, "_brand", {}) or {}).get("title")
        return custom if custom else "Dr.Prepper - offline drug & overdose reference"

    @property
    def brand_title(self) -> str:
        return (getattr(self, "_brand", {}) or {}).get("title") or "Dr.Prepper"

    def _load_brand(self):
        """Title and icon set in the console (Admin > Branding), shared through the kiosk folder."""
        import json
        from .intake_client import kiosk_dir
        p = kiosk_dir() / "branding.json"
        try:
            sig = p.stat().st_mtime_ns
        except OSError:
            sig = None
        if sig == getattr(self, "_brand_sig", "unset"):
            return
        self._brand_sig = sig
        try:
            self._brand = json.loads(p.read_text()) if sig else {}
        except (OSError, ValueError):
            self._brand = {}
        tz = self._brand.get("timezone")
        if tz and tz != os.environ.get("TZ") and hasattr(time, "tzset"):   # unit time zone, set in the console
            os.environ["TZ"] = tz
            time.tzset()
            self._tick()
        icon = self._brand.get("icon") if self._brand.get("show", "both") in ("both", "icon") else ""
        self.top.icon = str(kiosk_dir() / icon) if icon and (kiosk_dir() / icon).exists() else ""
        if self.sm.current == "home":
            self.set_title(self.home_title)
        self.title = self.brand_title

    def _check_data(self, *_):
        """Switch to a newly built drug database and keep the sample warning current."""
        self._load_brand()
        self.db.refresh()
        if self.db.generation != self._gen:
            self._gen = self.db.generation
            self.meta = self.db.meta()
            if self.sm.current in ("search", "list"):
                s = self.sm.get_screen(self.sm.current)
                if hasattr(s, "run_search"):
                    s.run_search()
        if not self.db.is_sample:
            self.root_widget.sample_text = ""
            return
        st = self.db.ingest_status() or {}
        if st.get("state") in ("downloading", "building"):
            more = f"Full FDA database: {st.get('detail', '')}. Switches over automatically."
        elif st.get("state") == "failed":
            more = "Full FDA download failed. Connect to the internet and run the drug database download."
        else:
            more = "Full FDA database not downloaded yet."
        self.root_widget.sample_text = f"[b]SAMPLE DRUG DATA - not a real drug reference.[/b]  {more}"

    def set_title(self, t: str):
        self.top.title = t

    def _push(self, screen: str, title: str, restore=None):
        cur = self.sm.current
        self.history.append((cur, self.top.title, getattr(self, "_restore", None)))
        self.history = self.history[-40:]
        self._restore = restore
        self.sm.current = screen
        self.set_title(title)

    def show(self, name: str):
        titles = {"calc": "Dose math", "about": "About"}
        if name == "about":
            self.sm.get_screen("about").fill(self.meta)
        self._push(name, titles.get(name, name))

    def open_intake(self, concern: str = "overdose"):
        s = self.sm.get_screen("intake")
        s.reset(concern)
        self._push("intake", "Incoming patient report")

    def intake_sent(self, pnum: str, report: dict):
        done = self.sm.get_screen("intake_done")
        done.show(pnum, report)
        self.history = [h for h in self.history if h[0] not in ("intake", "intake_done")]
        self.sm.current = "intake_done"
        self.set_title("Sent to triage")

    def go_home(self):
        self.history.clear()
        self._restore = None
        self.sm.current = "home"
        self.set_title(self.home_title)

    def go_back(self):
        if not self.history:
            return self.go_home()
        screen, title, restore = self.history.pop()
        if restore:
            restore()
        self._restore = restore
        self.sm.current = screen
        self.set_title(title)

    def _hw_key(self, _win, key, *_):
        if key == 27:  # Esc / Android back
            self.go_back()
            return True
        return False

    # ---- openers
    def open_search(self, q: str):
        s = self.sm.get_screen("search")
        s.ids.q.text = q
        s.keyboard_visible = True
        self._push("search", "Search")
        Clock.schedule_once(lambda *_: setattr(s.ids.q, "focus", True), 0.05)

    def open_browse(self):
        s = self.sm.get_screen("list")
        s.show_letters = True
        s.ids.rv.data = []
        self._push("list", "Browse A-Z", restore=lambda: setattr(s, "show_letters", True))
        s.load_letter("A")

    def open_tox_index(self):
        s = self.sm.get_screen("list")
        s.show_letters = False
        s.load_tox()

        def restore():
            s.show_letters = False
            s.load_tox()
        self._push("list", "Overdose cards", restore=restore)

    def open_tox(self, slug: str):
        t = self.db.get_tox(slug)
        if not t:
            return
        s = self.sm.get_screen("detail")
        s.show_tox(t, self.meta.get("disclaimer", ""))
        self._push("detail", t["name"], restore=lambda: s.show_tox(t, self.meta.get("disclaimer", "")))

    def open_drug(self, drug_id: int):
        d = self.db.get_drug(drug_id)
        if not d:
            return
        s = self.sm.get_screen("detail")
        s.show_drug(d)
        self._push("detail", d["display"], restore=lambda: s.show_drug(d))

    def open_ref(self, ref):
        if not ref:
            return
        kind, key = ref
        if kind == "tox":
            self.open_tox(key)
        elif kind == "drug":
            self.open_drug(int(key))

    def on_start(self):
        self.go_home()

    # ---- screenshot mode (CI / docs)
    def _screenshots(self, *_):
        out = self.screenshot_dir
        out.mkdir(parents=True, exist_ok=True)
        steps = [
            ("01-home", lambda: self.go_home()),
            ("02-search", lambda: (self.open_search("nalox"), self.sm.get_screen("search").run_search())),
            ("03-tox-unknown", lambda: self.open_tox("unknown-overdose")),
            ("04-tox-opioids", lambda: self.open_tox("opioids")),
            ("05-tox-index", lambda: self.open_tox_index()),
            ("06-drug", lambda: self._first_drug()),
            ("07-browse", lambda: self.open_browse()),
            ("08-calc", lambda: self._calc_demo()),
            ("09-about", lambda: self.show("about")),
        ]

        def run(i=0):
            if i >= len(steps):
                self.stop()
                return
            name, fn = steps[i]
            fn()
            Clock.schedule_once(lambda *_: (self.root.export_to_png(str(out / f"{name}.png")),
                                            Clock.schedule_once(lambda *_: run(i + 1), 0.2)), 0.6)
        run()

    def _first_drug(self):
        hits = self.db.search("acetaminophen", include_tox=False).drugs or self.db.browse("a")
        if hits:
            self.open_drug(hits[0].id)

    def _calc_demo(self):
        self.show("calc")
        s = self.sm.get_screen("calc")
        s.ids.wt.text, s.ids.mgkg.text, s.ids.conc.text, s.ids.maxd.text = "154", "0.1", "1", "4"
        for b in s.ids.wt.parent.children:
            if getattr(b, "text", "") == "lb":
                b.state = "down"
                s.ids.unit_kg.state = "normal"
        s.recalc()
