"""BookTick: phone alerts the moment movie tickets open on District.

This file lays out the page and wires its events. The work happens in booktick/:
district.py reads District, engine.py checks watchers, alerts.py sends alerts,
storage.py saves them, access.py decides who can see and change what, views.py and
handlers.py build the page, and static/ holds its CSS and JS.
"""
import gradio as gr

from booktick import handlers as h
from booktick.actions import register as register_actions
from booktick.config import (APP_NAME, DEFAULT_CINEMA_LABEL, DEFAULT_CITY, DEFAULT_URL, FORMAT_CHOICES,
                             MAX_INTERVAL, MIN_INTERVAL)
from booktick.core import state, today_ist
from booktick.district import city_label
from booktick.engine import start_engine
from booktick.storage import start_storage
from booktick.theme import CSS, HEAD, favicon_file, theme
from booktick.views import (HELP_HTML, about_html, admin_status_html, cinema_skeleton, date_picker_html,
                            engine_note_html, footer_html, header_html, icon, logs_skeleton, status_skeleton,
                            step_head, watchers_skeleton)

# ---------------------------------------------------------------------------
# ZeroGPU compatibility
# ---------------------------------------------------------------------------
# This app never needs a GPU. A ZeroGPU Space refuses to start unless at least one
# function is decorated with @spaces.GPU, so we decorate a no-op that nothing calls.
# Never put the decorator on a real button handler: every click spends GPU quota.
try:
    import spaces

    @spaces.GPU
    def _zerogpu_placeholder():
        pass
except ImportError:
    pass


def live_region(region, html):
    """A block whose content the browser patches in place (see BookTick.render in app.js).

    Python never re-renders it. Fresh HTML goes into a hidden textbox, and its change
    event hands the HTML to the page script.
    """
    gr.HTML(f'<div class="bt-live-root" data-region="{region}">{html}</div>', elem_classes="bt-root-html")
    source = gr.Textbox(value=html, elem_classes="bt-hidden", show_label=False, container=False, max_lines=1)
    source.change(None, inputs=[source], js=f"(h) => (window.BookTick ? window.BookTick.render('{region}', h) : [])")
    return source


def hidden_box(elem_id=None, value=""):
    return gr.Textbox(value=value, elem_id=elem_id, elem_classes="bt-hidden", show_label=False, container=False, max_lines=1)


JS = "window.BookTick"  # every js= hook checks the page script loaded first

with gr.Blocks(title=f"{APP_NAME} · Movie ticket alerts for District", theme=theme, css=CSS, head=HEAD) as demo:
    # Per-tab, server-side: whose watchers these are, and whether this tab is the admin.
    session = gr.State({})

    gr.HTML(header_html(), elem_classes="bt-root-html")
    status_box = live_region("status", status_skeleton())

    with gr.Tabs(elem_id="bt-tabs"):
        # --- New watcher -----------------------------------------------------
        with gr.Tab("New watcher", id="new", elem_id="tab-new"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=3, min_width=340, elem_classes="bt-stack"):
                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head("1", "Pick your show", "Choose your city and cinema, then the movie you're waiting for."))
                        with gr.Row():
                            city_input = gr.Dropdown(
                                label="City", choices=[(city_label(DEFAULT_CITY), DEFAULT_CITY)], value=DEFAULT_CITY,
                                info="Type to search 790+ cities.", scale=2, min_width=180, elem_id="bt-city",
                                elem_classes=["bt-dd", "bt-dir"],
                            )
                            cinema_input = gr.Dropdown(
                                label="Cinema", choices=[(DEFAULT_CINEMA_LABEL, DEFAULT_URL)], value=DEFAULT_URL,
                                info="Type part of the name or the area, or pick Any cinema.", scale=3, min_width=240,
                                elem_id="bt-cinema-pick", elem_classes=["bt-dd", "bt-dir"],
                            )
                        cinema_info = gr.HTML(cinema_skeleton(), elem_id="bt-cinema-info", elem_classes="bt-root-html")
                        movie_filter_input = gr.Dropdown(
                            label="Movie", choices=["Ramayana"], value=["Ramayana"], multiselect=True, allow_custom_value=True,
                            elem_id="bt-movie", elem_classes=["bt-dd", "bt-dir"],
                            info="Movies at this cinema come first, then the rest of the city. Not listed yet? "
                                 "Type the name and press Enter. Part of a name is enough.",
                        )
                        with gr.Row():
                            format_filter_input = gr.Dropdown(
                                label="Format", choices=FORMAT_CHOICES, value="IMAX", allow_custom_value=True,
                                info="Pick one or type your own.", min_width=160, elem_classes="bt-dd",
                            )
                            with gr.Column(min_width=200):
                                gr.HTML(date_picker_html(today_ist()), elem_classes="bt-root-html")
                        slots_input = gr.CheckboxGroup(
                            label="Show time", choices=h.SLOT_CHOICES, value=[], elem_id="bt-slots",
                            info="Leave all unticked for any time. Morning is before 12 PM, afternoon 12–4, "
                                 "evening 4–8, night after 8.",
                        )
                        with gr.Accordion("Or paste a cinema link", open=False, elem_classes="bt-accordion") as link_accordion:
                            cin_url_input = gr.Textbox(
                                label="Cinema page on District",
                                value=DEFAULT_URL,
                                max_lines=1,
                                placeholder="https://www.district.in/movies/...",
                                info="For a cinema that isn't in the list: open it on district.in and copy the link from the address bar.",
                            )
                        preview_btn = gr.Button("Preview shows", variant="secondary")
                        preview_out = gr.HTML(elem_id="bt-preview")

                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head("2", "Where should we alert you?", "Fill in one or more. ntfy is the quickest to set up."))
                        user_name_input = gr.Textbox(
                            label="Your name", value="Me", placeholder="e.g. Arun", max_lines=1,
                            info="Shown on the watcher card so you can spot yours.",
                        )
                        with gr.Tabs(elem_id="bt-channel-tabs"):
                            with gr.Tab("ntfy (easiest)"):
                                gr.HTML('<p class="bt-hint">Install the free <b>ntfy</b> app, tap <b>+</b> and subscribe to a topic. Enter the same topic here.</p>')
                                ntfy_topic_input = gr.Textbox(label="ntfy topic", placeholder="e.g. arun_tickets_982", max_lines=1)
                            with gr.Tab("Telegram"):
                                gr.HTML('<p class="bt-hint">Create a bot with <b>@BotFather</b> and send it any message. Get your chat ID from <b>@userinfobot</b>.</p>')
                                with gr.Row():
                                    tg_token_input = gr.Textbox(label="Bot token", type="password", placeholder="123456:ABC-DEF…")
                                    tg_chat_input = gr.Textbox(label="Chat ID", placeholder="123456789", max_lines=1)
                            with gr.Tab("Discord"):
                                gr.HTML('<p class="bt-hint">In your server: <b>Edit channel → Integrations → Webhooks → New webhook</b>, then copy its URL.</p>')
                                disc_hook_input = gr.Textbox(label="Webhook URL", placeholder="https://discord.com/api/webhooks/…", max_lines=1)

                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head("3", "When tickets open", "Choose how many alerts to get, send a test, then start watching."))
                        max_alerts_slider = gr.Slider(
                            label="Alerts when bookings open", minimum=1, maximum=20, value=5, step=1,
                            info="The first alert plus reminders, one per check, until you tap a button in one. "
                                 "After that, BookTick still tells you if seats free up or bookings close and reopen.",
                        )
                        with gr.Row(elem_classes="bt-cta-row"):
                            test_notify_btn = gr.Button("Send test alert", variant="secondary", scale=1, min_width=150)
                            add_task_btn = gr.Button("Start watching", variant="primary", scale=2, min_width=180)
                        add_result_lbl = gr.HTML(elem_id="bt-add-result")

                with gr.Column(scale=2, min_width=300, elem_classes="bt-aside"):
                    gr.HTML(HELP_HTML, elem_classes="bt-root-html")

        # --- Watchers --------------------------------------------------------
        with gr.Tab("Watchers", id="watchers", elem_id="tab-watchers") as watchers_tab:
            watchers_box = live_region("watchers", watchers_skeleton())

        # --- Activity --------------------------------------------------------
        with gr.Tab("Activity", id="activity", elem_id="tab-activity") as activity_tab:
            with gr.Row(elem_classes="bt-log-bar"):
                gr.HTML('<div class="bt-section-title"><h3>Activity log</h3><p>Newest first: every check result, alert and change for your watchers.</p></div>')
                copy_log_btn = gr.Button("Copy log", size="sm", elem_id="bt-copy-log", scale=0, min_width=110)
                clear_logs_btn = gr.Button("Clear log", size="sm", scale=0, min_width=110, visible=False)
            log_box = gr.HTML(logs_skeleton(), elem_id="bt-log")

        # --- Settings --------------------------------------------------------
        with gr.Tab("Settings", id="settings", elem_id="tab-settings"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=3, min_width=320, elem_classes="bt-stack"):
                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head(icon("activity"), "Engine", "How often BookTick checks District, for every watcher."))
                        engine_note = gr.HTML(engine_note_html({}))
                        with gr.Column(visible=False) as engine_controls:
                            interval_slider = gr.Slider(
                                label="Check every (seconds)", minimum=MIN_INTERVAL, maximum=MAX_INTERVAL, step=5,
                                value=state["interval_sec"],
                                info="Very frequent checks can get the Space blocked by District.",
                            )
                            toggle_engine_btn = gr.Button("Pause all checking", variant="secondary")
                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head(icon("shield"), "Admin", "For whoever runs this Space."))
                        admin_status = gr.HTML(admin_status_html({}))
                        with gr.Row(elem_classes="bt-admin-row", visible=False) as unlock_row:
                            admin_key_input = gr.Textbox(label="Admin key", type="password", max_lines=1, scale=3,
                                                         placeholder="The ADMIN_KEY secret")
                            admin_btn = gr.Button("Unlock", variant="primary", scale=1, min_width=110)
                        lock_btn = gr.Button("Sign out of admin", variant="secondary", visible=False)
                with gr.Column(scale=2, min_width=280):
                    gr.HTML(about_html(), elem_classes="bt-root-html")

    gr.HTML(footer_html(), elem_classes="bt-root-html")

    # Hidden bridges for the page script (see static/app.js): who you are, the admin key to
    # remember, the date picked in the calendar, and the buttons on the watcher cards.
    identity_box = hidden_box("bt-identity")
    admin_store = hidden_box("bt-admin-store")
    target_date_input = hidden_box("bt-date", today_ist())
    action_box = hidden_box("bt-action")
    action_btn = gr.Button("Run action", elem_id="bt-action-btn", elem_classes="bt-hidden")

    # --- Events ---------------------------------------------------------------
    pick_inputs = [cinema_input, cin_url_input, movie_filter_input, format_filter_input, slots_input, target_date_input]
    channels = [ntfy_topic_input, tg_token_input, tg_chat_input, disc_hook_input]
    admin_views = [engine_controls, unlock_row, lock_btn, clear_logs_btn]
    live = [status_box, watchers_box, log_box]

    city_input.input(
        h.on_city_change, inputs=[city_input, movie_filter_input],
        outputs=[cinema_input, movie_filter_input, cinema_info, link_accordion, cin_url_input],
    )
    cinema_input.input(
        h.on_cinema_change, inputs=[city_input, cinema_input, movie_filter_input],
        outputs=[cin_url_input, movie_filter_input, cinema_info], show_progress="hidden",
    )
    cin_url_input.input(
        h.on_link_input, inputs=[cin_url_input, city_input, movie_filter_input],
        outputs=[cinema_input, movie_filter_input, cinema_info], show_progress="hidden", trigger_mode="always_last",
    )
    preview_btn.click(h.preview_shows, inputs=pick_inputs, outputs=[preview_out])
    test_notify_btn.click(
        h.test_user_notification, inputs=[session, user_name_input, cinema_input, cin_url_input] + channels,
        outputs=[add_result_lbl],
    )
    add_task_btn.click(
        h.add_new_watcher,
        inputs=[session, user_name_input] + pick_inputs[:5] + [target_date_input, max_alerts_slider] + channels,
        outputs=[add_result_lbl, watchers_box, status_box],
    )

    action_btn.click(
        h.run_watcher_action, inputs=[action_box, session],
        outputs=[session, status_box, watchers_box, engine_note, toggle_engine_btn],
        js=f"(v, s) => [{JS} ? {JS}.take() : '', s]",
        show_progress="hidden", trigger_mode="multiple",
    )
    watchers_tab.select(h.refresh_watchers, inputs=[session], outputs=[session, watchers_box], show_progress="hidden")
    activity_tab.select(h.refresh_logs, inputs=[session], outputs=[session, log_box], show_progress="hidden")
    copy_log_btn.click(None, js=f"() => ({JS} ? {JS}.copyLog() : [])")
    clear_logs_btn.click(h.clear_logs, inputs=[session], outputs=[log_box], show_progress="hidden")

    toggle_engine_btn.click(
        h.toggle_engine, inputs=[session],
        outputs=[session, engine_note, toggle_engine_btn, status_box, watchers_box],
    )
    interval_slider.release(h.change_interval, inputs=[interval_slider, session],
                            outputs=[engine_note, interval_slider, status_box])
    for trigger in (admin_btn.click, admin_key_input.submit):
        trigger(h.admin_login, inputs=[admin_key_input, session],
                outputs=[session, admin_store, admin_key_input, admin_status, engine_note] + admin_views + live)
    lock_btn.click(h.admin_logout, inputs=[session],
                   outputs=[session, admin_store, admin_status, engine_note] + admin_views + live)
    admin_store.change(None, inputs=[admin_store], js=f"(k) => ({JS} ? {JS}.storeAdmin(k) : [])")

    # Python sets the date too (on page load), so keep the calendar's button text in step.
    target_date_input.change(None, inputs=[target_date_input],
                             js=f"(v) => ({JS} && {JS}.date ? {JS}.date.sync(v) : [])")

    timer = gr.Timer(3)
    timer.tick(h.poll_status, inputs=[session], outputs=[session] + live, show_progress="hidden")
    demo.load(
        h.on_page_load, inputs=[identity_box],
        outputs=[session, admin_store, target_date_input, interval_slider, engine_note, toggle_engine_btn,
                 admin_status] + admin_views + live,
        js=f"() => ({JS} ? {JS}.identity() : '')",
        show_progress="hidden",
    )
    # Separate so a slow District page never holds up the rest of the page.
    demo.load(
        h.load_directory, inputs=[city_input, cin_url_input, movie_filter_input],
        outputs=[city_input, cinema_input, movie_filter_input, cinema_info],
        show_progress="hidden",
    ).then(None, js=f"() => ({JS} ? {JS}.booted() : [])")  # runs even if loading failed

# --- Launch -------------------------------------------------------------------
start_storage()
start_engine()
# launch() must start the app: ZeroGPU Spaces only accept apps started that way.
demo.queue(default_concurrency_limit=8).launch(server_name="0.0.0.0", server_port=7860, favicon_path=favicon_file(),
                                               prevent_thread_lock=True)
register_actions(demo.app)  # the addresses the buttons in alerts call (see booktick/actions.py)
demo.block_thread()
