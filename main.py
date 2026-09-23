import customtkinter as ctk
import os
import json
import ctypes
import ctypes.wintypes as wintypes
import math
import sys
import tkinter as tk

from pathlib import Path
from tkinter import filedialog
from PIL import Image, ImageTk, ImageChops

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

DESKTOP = Path.home() / "Desktop"

# When PyInstaller bundles this into a single .exe, `__file__` points
# inside a temporary extraction folder (a different location every
# run), not next to the actual .exe - so workspaces.json would never
# be found on a second launch. `sys.frozen` is set by PyInstaller at
# runtime, and `sys.executable` correctly points at the real .exe's
# location in that case, so use that instead when it's set.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "workspaces.json"

GRID_COLUMNS = 3
CARD_CORNER_RADIUS = 28

# Colors reused for both hover feedback and keyboard-selection feedback,
# so the two visually mean the same thing ("this is the active item").
CARD_SELECTED_COLOR = "#4A4A4A"    # matches card hover color
BUTTON_SELECTED_COLOR = "#4A4A4A"  # matches button hover_color
TEXT_COLOR_LIGHT_GRAY = "#D6D6D6"  # lighter than the old #AAAAAA/#BBBBBB

# Windows 11 DWM attribute IDs used for the acrylic title bar.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38    # Windows 11 build 22621+ (22523 Insider)
DWMSBT_TRANSIENTWINDOW = 3        # "Acrylic" backdrop material

GWLP_WNDPROC = -4
WM_NCACTIVATE = 0x0086

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

# Must survive for the life of the window - ctypes does not keep a
# Python callback object alive on its own, and if it gets garbage
# collected Windows will crash calling a freed function pointer.
_wndproc_refs = {}


BACKGROUND_COLOR = "#080808"


def force_window_always_active(window):
    """Make Windows always paint this window as active/focused.

    Standard technique for custom-chrome apps: intercept the
    WM_NCACTIVATE message and force it to report "active" every time,
    regardless of real focus state. This stops the Acrylic backdrop
    (and, on older setups, the title bar) from dimming when the app
    is clicked into but isn't the OS foreground window in the usual
    sense.

    This only affects how the non-client area is *painted* - it does
    not change actual keyboard focus routing, so typing/clicking still
    behaves normally.
    """
    try:
        user32 = ctypes.windll.user32

        hwnd = user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()

        WNDPROCTYPE = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM
        )

        user32.CallWindowProcW.restype = ctypes.c_long
        user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]

        user32.GetWindowLongPtrW.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]

        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_void_p,
        ]

        old_wndproc = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

        def new_wndproc(hwnd_param, msg, wparam, lparam):
            if msg == WM_NCACTIVATE:
                wparam = 1  # force TRUE: always report "active"
            return user32.CallWindowProcW(
                old_wndproc, hwnd_param, msg, wparam, lparam
            )

        new_wndproc_c = WNDPROCTYPE(new_wndproc)

        user32.SetWindowLongPtrW(
            hwnd,
            GWLP_WNDPROC,
            ctypes.cast(new_wndproc_c, ctypes.c_void_p)
        )

        # Keep both the callback and the original proc pointer alive.
        _wndproc_refs[hwnd] = (new_wndproc_c, old_wndproc)

    except Exception as error:
        print(f"Could not force always-active window state: {error}")


def refresh_window_frame(window):
    """Ask Windows to recompute this window's non-client frame and
    hit-test regions - the invisible strip you grab at an edge to
    resize the window.

    Best-effort fix, not verified on a live Windows session: this
    targets a known class of Windows bug where certain window-style
    tricks (including the `-transparentcolor` chroma-key used here)
    can leave the cached resize hit-test region stale after the
    window has actually been resized once, so the resize cursor stops
    appearing at the edges even though the window itself is still
    genuinely resizable. SWP_FRAMECHANGED is the standard way to tell
    Windows "recalculate this" without actually moving or resizing
    anything. If this doesn't fix it, the next thing to try is
    disabling force_window_always_active() to check whether that hook
    is the actual cause instead.
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]

        hwnd = user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()

        user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            None,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
        )
    except Exception as error:
        print(f"Could not refresh window frame: {error}")


def enable_acrylic(window):
    """Make the window's own background color a chroma-key "hole" so
    the Windows 11 Acrylic backdrop (composited by DWM) shows through
    it.

    Trade-off (kept deliberately, per request): this turns the whole
    window into a color-key layered window, which can't alpha-blend.
    That means anti-aliased text/rounded corners are impossible here -
    which is why the rest of this file draws text and shapes using
    the hard-edged, blend-free techniques below instead of relying on
    customtkinter's normal smoothed rendering.
    """
    try:
        window.wm_attributes("-transparentcolor", BACKGROUND_COLOR)
    except tk.TclError:
        pass

    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]

        hwnd = user32.GetParent(window.winfo_id())

        if not hwnd:
            hwnd = window.winfo_id()

        hwnd_ptr = ctypes.c_void_p(hwnd)

        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        dark_mode = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(
            hwnd_ptr,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(dark_mode),
            ctypes.sizeof(dark_mode)
        )

        backdrop = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        result = dwmapi.DwmSetWindowAttribute(
            hwnd_ptr,
            DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(backdrop),
            ctypes.sizeof(backdrop)
        )

        if result != 0:
            print(
                f"Could not enable Windows acrylic backdrop "
                f"(hr=0x{result & 0xFFFFFFFF:08X})."
            )

    except Exception as error:
        print(f"Could not enable acrylic: {error}")


# ---------------------------------------------------------------------
# Blend-free drawing helpers.
#
# Everything below draws with hard edges on purpose: a transparent
# color-key window can't alpha-blend, so a normal anti-aliased rounded
# rectangle or glyph edge just renders jagged. A many-sided polygon
# reads as "rounded" without needing blending, and stamping text at a
# few pixel offsets in black behind the real text reads as a clean
# border without needing blending either.
# ---------------------------------------------------------------------

def resize_logo_without_fringing(image, size):
    """Resize an RGBA logo without the color-fringe halo PIL's default
    resize can introduce around transparent edges.

    PIL's resampling filters interpolate the RGB and alpha channels
    independently. At a transparent edge, invisible pixels can still
    carry arbitrary leftover RGB values, and those leak into nearby
    visible pixels during interpolation - showing up as a faint
    colored outline around the icon. Premultiplying alpha before the
    resize (and dividing it back out after) prevents that leakage.
    Pure PIL, no extra dependency - icons here are small (<=90px) so
    the per-pixel Python loop is effectively instant.
    """
    r, g, b, a = image.split()

    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)

    premultiplied = Image.merge("RGBA", (r, g, b, a)).resize(
        size, Image.LANCZOS
    )

    pixels = list(premultiplied.getdata())
    unpremultiplied = [
        (0, 0, 0, 0) if alpha == 0 else (
            min(255, red * 255 // alpha),
            min(255, green * 255 // alpha),
            min(255, blue * 255 // alpha),
            alpha
        )
        for red, green, blue, alpha in pixels
    ]

    result = Image.new("RGBA", size)
    result.putdata(unpremultiplied)
    return result


def snap_alpha_to_binary(image, threshold=128):
    """Force every pixel of an RGBA image fully transparent or fully
    opaque - no partial values in between.

    Under `-transparentcolor`, Tk composites a photo's semi-transparent
    edge pixels against whatever's underneath *before* the window is
    ever drawn. When that underlying color happens to be the exact
    chroma-key value, the blended result is close to - but not
    exactly - that value, so instead of joining the transparent "hole"
    it renders as a small solid, slightly-off-color pixel. That's the
    black outline around logos: a ring of blended near-black pixels
    exactly where the source PNG's anti-aliased edge used to be.
    Removing partial alpha entirely removes anything that could blend
    like that in the first place.
    """
    r, g, b, a = image.split()
    a = a.point(lambda p: 255 if p >= threshold else 0)
    return Image.merge("RGBA", (r, g, b, a))


def rounded_rect_points(x1, y1, x2, y2, radius, segments=10):
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = []

    def arc(cx, cy, start_deg, end_deg):
        for i in range(segments + 1):
            t = start_deg + (end_deg - start_deg) * i / segments
            rad = math.radians(t)
            points.append(cx + radius * math.cos(rad))
            points.append(cy + radius * math.sin(rad))

    arc(x2 - radius, y1 + radius, 270, 360)  # top-right
    arc(x2 - radius, y2 - radius, 0, 90)     # bottom-right
    arc(x1 + radius, y2 - radius, 90, 180)   # bottom-left
    arc(x1 + radius, y1 + radius, 180, 270)  # top-left

    return points


def create_rounded_button(
    parent,
    text,
    font,
    command,
    width,
    height,
    corner_radius=10,
    idle_fill=BACKGROUND_COLOR,
    hover_fill="#252525",
    text_color=TEXT_COLOR_LIGHT_GRAY,
    is_selected_check=None
):
    """A small canvas-based button: rounded (blend-free) background.

    A real CTkButton's rounded corners have the same anti-aliasing
    problem as cards did under the transparent window, so this draws
    its own background the same way create_workspace_card does.
    """
    button_canvas = tk.Canvas(
        parent,
        width=width,
        height=height,
        bg=BACKGROUND_COLOR,
        highlightthickness=0,
        bd=0
    )

    state = {"highlighted": False}

    def render():
        button_canvas.delete("all")
        current_idle = idle_fill() if callable(idle_fill) else idle_fill

        # The canvas's own rectangular background is what actually
        # shows in the four corner slivers outside the rounded
        # polygon - if it's left at its original fixed value, those
        # corners keep showing the raw transparent chroma-key color
        # even once this button is supposed to match something else
        # (e.g. a hovered card behind it), punching a small square
        # hole right through the highlight. Keeping it synced to
        # whatever should currently be showing there fixes that.
        button_canvas.configure(bg=current_idle)

        if state["highlighted"]:
            # Only draw the rounded shape when it needs to visually
            # pop out from its surroundings - the bg (current_idle)
            # still shows correctly in the corners either way.
            points = rounded_rect_points(
                1, 1, width - 1, height - 1, corner_radius
            )
            button_canvas.create_polygon(points, fill=hover_fill, outline="")

        button_canvas.create_text(
            width / 2, height / 2, text=text, font=font,
            fill=text_color, anchor="center"
        )

    def set_highlight(value):
        state["highlighted"] = value
        render()

    def on_enter(event=None):
        set_highlight(True)

    def on_leave(event=None):
        if is_selected_check is not None and is_selected_check():
            return
        set_highlight(False)

    def on_click(event=None):
        command()

    button_canvas.bind("<Button-1>", on_click)
    button_canvas.bind("<Enter>", on_enter)
    button_canvas.bind("<Leave>", on_leave)

    button_canvas.set_highlight = set_highlight
    # Lets an external caller re-render this button (e.g. its idle
    # color depends on something outside it, like a card's own hover
    # state) without going through a real Enter/Leave/click event.
    button_canvas.refresh = render
    render()

    return button_canvas


class Workspace:

    def __init__(self, name, shortcut_path, logo_path=None):
        self.name = name
        self.shortcut_path = str(shortcut_path)
        self.logo_path = logo_path

    def launch(self):
        if not os.path.exists(self.shortcut_path):
            print(
                f"Workspace shortcut not found:\n"
                f"{self.shortcut_path}"
            )
            return

        try:
            os.startfile(self.shortcut_path)
        except OSError as error:
            print(
                f"Failed to launch workspace "
                f"'{self.name}': {error}"
            )

    def to_dict(self):
        return {
            "name": self.name,
            "shortcut_path": self.shortcut_path,
            "logo_path": self.logo_path
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["name"],
            data["shortcut_path"],
            data.get("logo_path")
        )


workspaces = []
card_widgets = []
selected_index = 0


def save_workspaces():
    with open(CONFIG_FILE, "w", encoding="utf-8") as file:
        json.dump(
            [workspace.to_dict() for workspace in workspaces],
            file,
            indent=4,
            ensure_ascii=False
        )


def load_workspaces():
    global workspaces

    workspaces = []

    if not CONFIG_FILE.exists():
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        print("Could not read workspaces.json, starting empty.")
        return

    if not isinstance(data, list):
        print("workspaces.json is malformed, starting empty.")
        return

    loaded = []

    for item in data:
        try:
            loaded.append(Workspace.from_dict(item))
        except (KeyError, TypeError) as error:
            print(f"Skipping malformed workspace entry: {error}")

    workspaces = loaded


def truncate_text_to_width(text, font_obj, max_width):
    """Return text as-is if it fits max_width, else elide with '…'."""

    if font_obj.measure(text) <= max_width:
        return text

    ellipsis = "…"
    low, high = 0, len(text)
    best = ellipsis

    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + ellipsis

        if font_obj.measure(candidate) <= max_width:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1

    return best


app = ctk.CTk()
app.title("My Workspace")

# Fixed, always-centered startup size. Chosen as a practical
# workaround for the resize-hit-test bug: starting already large
# means there's rarely a need to enlarge the window by dragging an
# edge in the first place.
WINDOW_WIDTH = 2000
WINDOW_HEIGHT = 1200

app.update_idletasks()
screen_width = app.winfo_screenwidth()
screen_height = app.winfo_screenheight()
window_x = max(0, (screen_width - WINDOW_WIDTH) // 2)
window_y = max(0, (screen_height - WINDOW_HEIGHT) // 2)

app.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{window_x}+{window_y}")
app.minsize(700, 550)
app.configure(fg_color=BACKGROUND_COLOR)


header = ctk.CTkFrame(
    app,
    fg_color="transparent"
)

header.pack(
    fill="x",
    padx=45,
    pady=(35, 10)
)

title_font = ctk.CTkFont(family="Segoe UI", size=24, weight="bold")

title_canvas = tk.Canvas(
    header,
    bg=BACKGROUND_COLOR,
    highlightthickness=0,
    bd=0,
    height=34
)

title_canvas.pack(fill="x")


def render_title(event=None):
    title_canvas.delete("all")
    title_canvas.create_text(
        1, 17, text="MY WORKSPACE", font=title_font,
        fill="#FFFFFF", anchor="w"
    )


title_canvas.bind("<Configure>", render_title)

# Fix: subtitle font bumped 15 -> 17 (+2), per request.
subtitle_font = ctk.CTkFont(family="Segoe UI", size=17)

subtitle_canvas = tk.Canvas(
    header,
    bg=BACKGROUND_COLOR,
    highlightthickness=0,
    bd=0,
    height=28
)

subtitle_canvas.pack(fill="x", pady=(2, 0))


def render_subtitle(event=None):
    subtitle_canvas.delete("all")
    subtitle_canvas.create_text(
        1, 14, text="What are you working on?", font=subtitle_font,
        fill=TEXT_COLOR_LIGHT_GRAY, anchor="w"
    )


subtitle_canvas.bind("<Configure>", render_subtitle)


# ---------------------------------------------------------------------
# Scrollable workspace grid: a plain tk.Canvas + CTkScrollbar, so we
# have full control over when the scrollbar appears/disappears and
# how mouse-wheel scrolling is wired up.
# ---------------------------------------------------------------------

scroll_container = ctk.CTkFrame(
    app,
    fg_color="transparent"
)

scroll_container.pack(
    fill="both",
    expand=True,
    padx=45,
    pady=(15, 20)
)

scroll_container.grid_rowconfigure(0, weight=1)
scroll_container.grid_columnconfigure(0, weight=1)

canvas = tk.Canvas(
    scroll_container,
    bg=BACKGROUND_COLOR,
    highlightthickness=0,
    bd=0
)

canvas.grid(
    row=0,
    column=0,
    sticky="nsew"
)

scrollbar = ctk.CTkScrollbar(
    scroll_container,
    orientation="vertical",
    command=canvas.yview,
    fg_color="transparent",
    button_color="#303030",
    button_hover_color="#454545"
)
# Not gridded here on purpose - update_scrollbar_visibility() decides
# whether it should be shown, based on whether content overflows.

canvas.configure(yscrollcommand=scrollbar.set)

workspace_frame = ctk.CTkFrame(
    canvas,
    fg_color="transparent"
)

workspace_window = canvas.create_window(
    (0, 0),
    window=workspace_frame,
    anchor="nw"
)


def update_scrollbar_visibility():
    canvas.update_idletasks()
    bbox = canvas.bbox("all")

    if not bbox:
        scrollbar.grid_remove()
        return

    content_height = bbox[3] - bbox[1]
    visible_height = canvas.winfo_height()

    if content_height > visible_height:
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
    else:
        scrollbar.grid_remove()
        canvas.yview_moveto(0)


def on_canvas_configure(event):
    # Keep the inner frame's width matched to the visible canvas
    # width so the card grid lays out correctly as the window resizes.
    canvas.itemconfig(workspace_window, width=event.width)
    update_scrollbar_visibility()


canvas.bind("<Configure>", on_canvas_configure)


def on_frame_configure(event=None):
    canvas.configure(scrollregion=canvas.bbox("all"))
    update_scrollbar_visibility()


workspace_frame.bind("<Configure>", on_frame_configure)


def on_mousewheel(event):
    if not scrollbar.winfo_ismapped():
        return
    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


def bind_mousewheel(widget):
    widget.bind("<MouseWheel>", on_mousewheel, add="+")


bind_mousewheel(canvas)
bind_mousewheel(workspace_frame)


def scroll_card_into_view(widget):
    canvas.update_idletasks()
    bbox = canvas.bbox("all")

    if not bbox:
        return

    content_height = bbox[3] - bbox[1]
    visible_height = canvas.winfo_height()

    if content_height <= visible_height:
        return

    widget_top = widget.winfo_y()
    widget_bottom = widget_top + widget.winfo_height()
    view_top = content_height * canvas.yview()[0]
    view_bottom = view_top + visible_height

    if widget_top < view_top:
        canvas.yview_moveto(widget_top / content_height)
    elif widget_bottom > view_bottom:
        canvas.yview_moveto((widget_bottom - visible_height) / content_height)


# ---------------------------------------------------------------------
# Keyboard navigation
# ---------------------------------------------------------------------

def total_slots():
    # Every workspace card, plus one extra slot for the Desktop button.
    return len(workspaces) + 1


def clear_selection_visual(index):
    if index < len(workspaces) and index < len(card_widgets):
        card_widgets[index].set_highlight(False)
    elif index == len(workspaces):
        desktop_button.set_highlight(False)


def apply_selection_visual(index):
    if index < len(workspaces) and index < len(card_widgets):
        card_widgets[index].set_highlight(True)
    elif index == len(workspaces):
        desktop_button.set_highlight(True)
        desktop_button.focus_set()


def set_selection(new_index, scroll_into_view=False):
    global selected_index

    slots = total_slots()

    if slots <= 0:
        return

    new_index = max(0, min(new_index, slots - 1))

    clear_selection_visual(selected_index)
    selected_index = new_index
    apply_selection_visual(selected_index)

    if scroll_into_view and selected_index < len(card_widgets):
        scroll_card_into_view(card_widgets[selected_index])


def move_selection_within_cards(new_index):
    if not workspaces:
        return

    new_index = max(0, min(new_index, len(workspaces) - 1))
    set_selection(new_index, scroll_into_view=True)


def on_tab(event):
    set_selection((selected_index + 1) % total_slots(), scroll_into_view=True)
    return "break"


def on_shift_tab(event):
    set_selection((selected_index - 1) % total_slots(), scroll_into_view=True)
    return "break"


def on_arrow_left(event):
    if selected_index < len(workspaces):
        move_selection_within_cards(selected_index - 1)
    return "break"


def on_arrow_right(event):
    if selected_index < len(workspaces):
        move_selection_within_cards(selected_index + 1)
    return "break"


def on_arrow_up(event):
    if selected_index < len(workspaces):
        move_selection_within_cards(selected_index - GRID_COLUMNS)
    return "break"


def on_arrow_down(event):
    if selected_index < len(workspaces):
        move_selection_within_cards(selected_index + GRID_COLUMNS)
    return "break"


def on_enter_key(event):
    if selected_index < len(workspaces):
        workspaces[selected_index].launch()
    elif selected_index == len(workspaces):
        show_desktop()
    return "break"


app.bind("<Tab>", on_tab)
app.bind("<Shift-Tab>", on_shift_tab)
app.bind("<Left>", on_arrow_left)
app.bind("<Right>", on_arrow_right)
app.bind("<Up>", on_arrow_up)
app.bind("<Down>", on_arrow_down)
app.bind("<Return>", on_enter_key)


def open_workspace_settings(workspace):

    settings = ctk.CTkToplevel(app)
    settings.title("Workspace settings")
    settings.geometry("420x400")
    settings.resizable(False, False)
    settings.transient(app)
    settings.grab_set()

    container = ctk.CTkFrame(
        settings,
        fg_color="transparent"
    )

    container.pack(
        fill="both",
        expand=True,
        padx=30,
        pady=30
    )

    title = ctk.CTkLabel(
        container,
        text="Workspace settings",
        font=("Segoe UI", 20, "bold")
    )

    title.pack(
        anchor="w",
        pady=(0, 20)
    )

    name_label = ctk.CTkLabel(
        container,
        text="Name",
        font=("Segoe UI", 13)
    )

    name_label.pack(
        anchor="w",
        pady=(0, 5)
    )

    name_entry = ctk.CTkEntry(
        container,
        height=38
    )

    name_entry.insert(
        0,
        workspace.name
    )

    name_entry.pack(
        fill="x"
    )

    logo_label = ctk.CTkLabel(
        container,
        text="Logo",
        font=("Segoe UI", 13)
    )

    logo_label.pack(
        anchor="w",
        pady=(20, 5)
    )

    logo_path_label = ctk.CTkLabel(
        container,
        text=(
            workspace.logo_path
            if workspace.logo_path
            else "No logo selected"
        ),
        text_color="#888888",
        anchor="w"
    )

    logo_path_label.pack(
        fill="x"
    )

    def choose_logo():
        path = filedialog.askopenfilename(
            title="Choose workspace logo",
            filetypes=[
                ("PNG images", "*.png")
            ]
        )

        if path:
            workspace.logo_path = path
            logo_path_label.configure(text=path)

    choose_logo_button = ctk.CTkButton(
        container,
        text="Choose PNG logo",
        height=35,
        command=choose_logo
    )

    choose_logo_button.pack(
        fill="x",
        pady=(8, 0)
    )

    def save_changes():
        new_name = name_entry.get().strip()

        if new_name:
            workspace.name = new_name

        save_workspaces()
        refresh_workspace_grid()
        settings.destroy()

    save_button = ctk.CTkButton(
        container,
        text="Save",
        height=40,
        command=save_changes
    )

    save_button.pack(
        fill="x",
        pady=(25, 0)
    )

    def delete_workspace():
        if workspace in workspaces:
            workspaces.remove(workspace)
            save_workspaces()
            refresh_workspace_grid()

        settings.destroy()

    delete_button = ctk.CTkButton(
        container,
        text="Delete workspace",
        height=35,
        fg_color="transparent",
        hover_color="#3A1717",
        text_color="#CC7777",
        command=delete_workspace
    )

    delete_button.pack(
        fill="x",
        pady=(8, 0)
    )


def create_workspace_card(workspace, row, column, index):

    card_width = 300
    card_height = 210

    card_canvas = tk.Canvas(
        workspace_frame,
        width=card_width,
        height=card_height,
        bg=BACKGROUND_COLOR,
        highlightthickness=0,
        bd=0
    )

    card_canvas.grid(
        row=row,
        column=column,
        padx=10,
        pady=10
    )

    name_font = ctk.CTkFont(family="Segoe UI", size=17, weight="bold")
    name_max_width = card_width - 40
    display_name = truncate_text_to_width(
        workspace.name,
        name_font,
        name_max_width
    )

    logo_pil = None

    if workspace.logo_path and os.path.exists(workspace.logo_path):
        try:
            logo_pil = Image.open(workspace.logo_path).convert("RGBA")
        except Exception as error:
            print(
                f"Could not load logo "
                f"{workspace.logo_path}: {error}"
            )
            logo_pil = None

    logo_photo = None  # must stay referenced or Tk garbage-collects it
    state = {"highlighted": False}

    def render():
        nonlocal logo_photo

        card_canvas.delete("all")

        fill = CARD_SELECTED_COLOR if state["highlighted"] else BACKGROUND_COLOR

        # Unlike the settings button (whose corners should blend into
        # whatever's behind it - the card), a card's own corners
        # should always blend into the constant app background, not
        # into the card's own highlight color. Syncing bg to `fill`
        # here made the background and the polygon the same color
        # whenever highlighted, which is what made the rounded shape
        # disappear into what looked like a plain square.
        card_canvas.configure(bg=BACKGROUND_COLOR)

        points = rounded_rect_points(
            2, 2, card_width - 2, card_height - 2, CARD_CORNER_RADIUS
        )
        card_canvas.create_polygon(points, fill=fill, outline="")

        if logo_pil is not None:
            logo_size = min(
                int(card_height * 0.40),
                int(card_width * 0.28),
                90
            )
            logo_size = max(38, logo_size)

            resized = resize_logo_without_fringing(
                logo_pil, (logo_size, logo_size)
            )
            resized = snap_alpha_to_binary(resized)
            logo_photo = ImageTk.PhotoImage(resized)

            card_canvas.create_image(
                card_width / 2,
                18 + logo_size / 2,
                image=logo_photo
            )

        card_canvas.create_text(
            card_width / 2,
            card_height - 55,
            text=display_name,
            font=name_font,
            fill="#FFFFFF",
            anchor="center"
        )

    def set_highlight(value):
        state["highlighted"] = value
        render()
        # The settings button is a separate overlaid canvas with its
        # own idle background - without this, it never learns the
        # card behind it just changed color and stays showing its own
        # (transparent) idle fill as a see-through patch.
        settings_button.refresh()

    render()

    settings_button = create_rounded_button(
        card_canvas,
        "•••",
        ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        lambda: open_workspace_settings(workspace),
        width=42,
        height=27,
        corner_radius=10,
        # Callable so its idle color always matches whatever the card
        # underneath is currently showing, not a fixed value.
        idle_fill=lambda: (
            CARD_SELECTED_COLOR if state["highlighted"] else BACKGROUND_COLOR
        ),
        hover_fill="#252525",
        text_color=TEXT_COLOR_LIGHT_GRAY
    )

    settings_button.place(
        relx=0.5,
        rely=1.0,
        anchor="s",
        y=-10
    )

    pending_leave_id = None

    def card_click(event=None):
        workspace.launch()

    def card_enter(event=None):
        nonlocal pending_leave_id

        if pending_leave_id is not None:
            card_canvas.after_cancel(pending_leave_id)
            pending_leave_id = None

        set_selection(index, scroll_into_view=False)

    def card_leave(event=None):
        nonlocal pending_leave_id

        def actually_leave():
            nonlocal pending_leave_id
            pending_leave_id = None
            if selected_index != index:
                set_highlight(False)

        pending_leave_id = card_canvas.after(40, actually_leave)

    card_canvas.bind("<Button-1>", card_click)
    card_canvas.bind("<Enter>", card_enter)
    card_canvas.bind("<Leave>", card_leave)
    bind_mousewheel(card_canvas)

    # settings_button handles its own click via `command=`; it only
    # needs hover styling here, not the click-through behavior.
    settings_button.bind("<Enter>", card_enter, add="+")
    settings_button.bind("<Leave>", card_leave, add="+")
    bind_mousewheel(settings_button)

    card_canvas.set_highlight = set_highlight

    return card_canvas


def refresh_workspace_grid():
    global card_widgets, selected_index

    # Reset any leftover highlight before rebuilding/reapplying it below.
    desktop_button.set_highlight(False)

    for widget in workspace_frame.winfo_children():
        widget.destroy()

    card_widgets = []

    if not workspaces:

        empty_label = ctk.CTkLabel(
            workspace_frame,
            text="No workspaces yet",
            font=("Segoe UI", 17),
            text_color="#777777",
            fg_color="transparent"
        )

        empty_label.place(
            relx=0.5,
            rely=0.5,
            anchor="center"
        )

        bind_mousewheel(empty_label)

        selected_index = 0
        apply_selection_visual(selected_index)

        app.after(10, update_scrollbar_visibility)
        return

    for column in range(GRID_COLUMNS):
        workspace_frame.grid_columnconfigure(
            column,
            weight=1
        )

    rows = (
        len(workspaces) + GRID_COLUMNS - 1
    ) // GRID_COLUMNS

    for row in range(rows):
        workspace_frame.grid_rowconfigure(
            row,
            weight=0
        )

    for index, workspace in enumerate(workspaces):

        row = index // GRID_COLUMNS
        column = index % GRID_COLUMNS

        card = create_workspace_card(
            workspace,
            row,
            column,
            index
        )

        card_widgets.append(card)

    selected_index = min(selected_index, len(workspaces) - 1)
    apply_selection_visual(selected_index)

    app.after(10, update_scrollbar_visibility)


def add_workspace():

    shortcut_path = filedialog.askopenfilename(
        title="Select PowerToys Workspace shortcut",
        initialdir=DESKTOP,
        filetypes=[
            ("Shortcut files", "*.lnk")
        ]
    )

    if not shortcut_path:
        return

    name = Path(shortcut_path).stem

    if name.endswith(" Workspace"):
        name = name[:-10]

    workspaces.append(
        Workspace(
            name=name,
            shortcut_path=shortcut_path
        )
    )

    save_workspaces()
    refresh_workspace_grid()


def show_desktop():

    user32 = ctypes.windll.user32

    user32.keybd_event(
        0x5B,
        0,
        0,
        0
    )

    user32.keybd_event(
        0x44,
        0,
        0,
        0
    )

    user32.keybd_event(
        0x44,
        0,
        2,
        0
    )

    user32.keybd_event(
        0x5B,
        0,
        2,
        0
    )


footer = ctk.CTkFrame(
    app,
    fg_color="transparent"
)

footer.pack(
    fill="x",
    padx=45,
    pady=(0, 30)
)

# Both footer buttons are canvas-based so their rounded corners don't
# have the same anti-aliasing problem CTkButton had under the
# transparent window. Font bumped from 13 to 15 (+2), per request.
footer_font = ctk.CTkFont(family="Segoe UI", size=15)

add_button = create_rounded_button(
    footer,
    "+ Add workspace",
    footer_font,
    add_workspace,
    width=180,
    height=36,
    corner_radius=10,
    idle_fill=BACKGROUND_COLOR,
    hover_fill=BUTTON_SELECTED_COLOR,
    text_color=TEXT_COLOR_LIGHT_GRAY
)

add_button.pack(side="left")

desktop_button = create_rounded_button(
    footer,
    "Desktop",
    footer_font,
    show_desktop,
    width=120,
    height=36,
    corner_radius=10,
    idle_fill=BACKGROUND_COLOR,
    hover_fill=BUTTON_SELECTED_COLOR,
    text_color=TEXT_COLOR_LIGHT_GRAY,
    # Keeps the mouse-driven hover from clearing the highlight while
    # Desktop is also the keyboard-selected slot - same fix as cards.
    is_selected_check=lambda: selected_index == len(workspaces)
)

desktop_button.pack(side="right")


def main():
    load_workspaces()
    refresh_workspace_grid()

    def apply_titlebar_effects():
        enable_acrylic(app)
        # Forces Windows to always paint this window as active, so the
        # Acrylic backdrop never dims - this is the actual fix for the
        # "background click turns it gray" issue. Installed once; it
        # patches the window's message handler rather than something
        # that needs reapplying.
        force_window_always_active(app)

    app.after(100, apply_titlebar_effects)

    # Kept as a light safety net: if anything still causes the
    # backdrop to look dimmed despite the hook above, this re-applies
    # the DWM attributes whenever the window reports becoming active.
    app.bind(
        "<Activate>",
        lambda event: app.after(10, lambda: enable_acrylic(app))
    )

    resize_refresh_id = {"id": None}

    def on_app_configure(event):
        if event.widget is not app:
            return

        if resize_refresh_id["id"] is not None:
            app.after_cancel(resize_refresh_id["id"])

        resize_refresh_id["id"] = app.after(
            150,
            lambda: refresh_window_frame(app)
        )

    app.bind("<Configure>", on_app_configure, add="+")

    app.mainloop()


if __name__ == "__main__":
    main()