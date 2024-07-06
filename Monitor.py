import json
import time
import threading
import base64
import random
import customtkinter as ctk
from PyQt5 import QtWidgets, QtWebEngineWidgets, QtCore
from PyQt5.QtCore import QUrl, pyqtSlot
import sys

def encode_password(password):
    return base64.b64encode(password.encode('utf-8')).decode('utf-8')

def decode_password(encoded_password):
    try:
        return base64.b64decode(encoded_password.encode('utf-8')).decode('utf-8')
    except (UnicodeDecodeError, base64.binascii.Error):
        return ""  # Return empty string if decoding fails

# Load configuration from config.json
default_config = {
    "url": "https://example.com",
    "base_check_interval": 1800,
    "email_sender": "you@example.com",
    "email_password": encode_password("password"),
    "email_receiver": "receiver@example.com",
    "smtp_server": "smtp.example.com",
    "smtp_port": 587,
    "saved_content_file": "saved_page.html",
    "internet_check_host": "8.8.8.8",
    "base_div_selector": "div.isotope-wrapper.grid-wrapper.half-gutter",
    "secondary_div_selector": "div.another-div-selector"
}

try:
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    with open('config.json', 'w') as config_file:
        json.dump(default_config, config_file, indent=4)
    config = default_config

# Extract settings from JSON
url = config.get("url", "")
base_check_interval = config.get("base_check_interval", 1800)
email_sender = config.get("email_sender", "")
email_password = decode_password(config.get("email_password", ""))
email_receiver = config.get("email_receiver", "")
smtp_server = config.get("smtp_server", "")
smtp_port = config.get("smtp_port", 587)
saved_content_file = config.get("saved_content_file", "saved_page.html")
internet_check_host = config.get("internet_check_host", "8.8.8.8")
base_div_selector = config.get("base_div_selector", "div.isotope-wrapper.grid-wrapper.half-gutter")
secondary_div_selector = config.get("secondary_div_selector", "div.another-div-selector")

class BrowserWindow(QtWidgets.QMainWindow):
    element_selected = pyqtSlot(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Element Selector')
        self.setGeometry(0, 0, 800, 600)
        self.browser = QtWebEngineWidgets.QWebEngineView()
        self.browser.setUrl(QUrl(url))
        self.setCentralWidget(self.browser)
        self.browser.page().profile().setHttpUserAgent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
        self.browser.page().loadFinished.connect(self.on_load_finished)

    def on_load_finished(self):
        self.browser.page().runJavaScript("""
            document.body.style.zoom = '80%';
            document.body.onmouseover = function(event) {
                event.target.style.outline = '2px solid green';
            };
            document.body.onmouseout = function(event) {
                event.target.style.outline = '';
            };
            document.body.onclick = function(event) {
                event.preventDefault();
                event.stopPropagation();
                var selectedElement = event.target.outerHTML;
                console.log(selectedElement);
                window.pywebview.api.select_element(selectedElement);
            };
        """)

    @pyqtSlot(str)
    def select_element(self, element_html):
        self.element_selected.emit(element_html)

class WebsiteMonitorApp:
    def __init__(self, root, monitor):
        self.root = root
        self.root.title("Website Monitor")
        self.running = False
        self.monitor_thread = None
        self.last_notified_content = None
 
        self.monitor = monitor

        self.setup_gui()

    def setup_gui(self):
        self.root.geometry("1024x768")
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        self.tab_control = ctk.CTkTabview(self.root)
        self.tab_control.grid(row=0, column=0, sticky="nsew")

        self.main_tab = self.tab_control.add("Main")
        self.settings_tab = self.tab_control.add("Settings")
        self.element_tab = self.tab_control.add("Element")

        self.setup_main_tab()
        self.setup_settings_tab()
        self.setup_element_tab()
        self.setup_theme_tab()
        
    def setup_main_tab(self):
        self.main_frame = ctk.CTkFrame(self.main_tab)
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.log_text = ctk.CTkTextbox(self.main_frame, width=800, height=200)
        self.log_text.pack(padx=10, pady=10)
        self.log_text.configure(state="disabled")

        self.html_text = ctk.CTkTextbox(self.main_frame, width=800, height=200)
        self.html_text.pack(padx=10, pady=10)
        self.html_text.configure(state="disabled")

        self.button_frame = ctk.CTkFrame(self.main_frame)
        self.button_frame.pack(pady=10)

        self.start_button = ctk.CTkButton(self.button_frame, text="Start", command=self.start_monitoring)
        self.start_button.grid(row=0, column=0, padx=5)

        self.stop_button = ctk.CTkButton(self.button_frame, text="Stop", command=self.stop_monitoring, state=ctk.DISABLED)
        self.stop_button.grid(row=0, column=1, padx=5)

        self.clear_button = ctk.CTkButton(self.button_frame, text="Clear", command=self.clear_logs)
        self.clear_button.grid(row=0, column=2, padx=5)
        
    def setup_theme_tab(self):
       # Theme selector combobox
        self.theme_label = ctk.CTkLabel(self.root, text="Theme:")
        self.theme_label.grid(pady=5)
        self.theme_combobox = ctk.CTkComboBox(self.root, values=["Dark", "Light"], command=self.change_theme)
        self.theme_combobox.grid(pady=5)
        self.theme_combobox.set("Dark")

    def setup_settings_tab(self):
        self.settings_frame = ctk.CTkFrame(self.settings_tab)
        self.settings_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.humanizer_checkbox = ctk.CTkCheckBox(self.settings_frame, text="Humanizer", command=self.toggle_humanizer)
        self.humanizer_checkbox.grid(row=0, column=0, pady=5, sticky="w")

        self.settings_elements = {}

        for i, (key, value) in enumerate(config.items()):
            label = ctk.CTkLabel(self.settings_frame, text=key)
            label.grid(row=i, column=0, padx=5, pady=5, sticky="e")

            entry = ctk.CTkEntry(self.settings_frame, width=300) if key in [
                "url", "email_sender", "email_receiver", "smtp_server"] else ctk.CTkEntry(self.settings_frame)
            entry.grid(row=i, column=1, padx=5, pady=5, sticky="w")
            entry.insert(0, str(value))

            self.settings_elements[key] = entry

        # Make password field hidden
        self.settings_elements["email_password"].configure(show="*")

        self.save_button = ctk.CTkButton(self.settings_frame, text="Save", command=self.save_settings)
        self.save_button.grid(row=len(config), column=0, columnspan=2, pady=10)

    def setup_element_tab(self):
        self.qt_app = QtWidgets.QApplication(sys.argv)

        self.element_frame = ctk.CTkFrame(self.element_tab)
        self.element_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.browser = QtWebEngineWidgets.QWebEngineView()
        self.browser.setUrl(QUrl(url))
        self.browser.page().profile().setHttpUserAgent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')

        self.qt_layout = QtWidgets.QVBoxLayout()
        self.qt_layout.addWidget(self.browser)

        self.container_widget = QtWidgets.QWidget()
        self.container_widget.setLayout(self.qt_layout)

        self.browser_container = QtWidgets.QWidget.createWindowContainer(self.browser.windowHandle())
        self.browser_container.setParent(self.browser)

        self.browser.page().loadFinished.connect(self.on_load_finished)
        
    def change_theme(self, event=None):
        ctk.set_appearance_mode(self.theme_combobox.get())

    def toggle_humanizer(self):
        global base_check_interval
        if self.humanizer_checkbox.get():
            base_check_interval = int(base_check_interval) + random.randint(1800, 3600)
        else:
            base_check_interval = int(base_check_interval)

    def log(self, message, level="INFO"):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert(ctk.END, f"{timestamp} {level}: {message}\n")
        self.log_text.configure(state="disabled")
        self.log_text.see(ctk.END)

    def start_monitoring(self):
        if not self.running:
            self.running = True
            self.monitor_thread = threading.Thread(target=self.monitor)
            self.monitor_thread.start()
            self.log("Monitoring started.", "INFO")
            self.start_button.configure(state=ctk.DISABLED)
            self.stop_button.configure(state=ctk.NORMAL)

    def stop_monitoring(self):
        if self.running:
            self.running = False
            self.monitor_thread.join(0)
            self.log("Monitoring stopped.", "INFO")
            self.start_button.configure(state=ctk.NORMAL)
            self.stop_button.configure(state=ctk.DISABLED)

    def clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete('1.0', ctk.END)
        self.log_text.configure(state="disabled")

    def save_settings(self):
        new_config = {}
        for key, entry in self.settings_elements.items():
            value = entry.get()
            if key == "email_password":
                value = encode_password(value)
            new_config[key] = value

        with open('config.json', 'w') as config_file:
            json.dump(new_config, config_file, indent=4)
        self.log("Settings saved.", "INFO")

    def on_load_finished(self):
        self.browser.page().runJavaScript("""
            document.addEventListener('click', function(event) {
                event.preventDefault();
                event.stopPropagation();
                let element = event.target;
                if (element) {
                    let element_html = element.outerHTML;
                    pywebview.api.select_element(element_html);
                }
            }, true);
        """)

    @pyqtSlot(str)
    def on_element_selected(self, element_html):
        self.log(f"Selected Element HTML: {element_html}", "INFO")
        self.html_text.configure(state="normal")
        self.html_text.insert(ctk.END, f"Selected Element:\n{element_html}\n\n")
        self.html_text.configure(state="disabled")
        self.tab_control.select("Main")

    def on_theme_change(self, event):
        if hasattr(web_monitor_app, 'browser_window'):
            web_monitor_app.browser_window.browser.update()

if __name__ == "__main__":
    root = ctk.CTk()
    monitor = None  # Replace with your monitor object initialization

    web_monitor_app = WebsiteMonitorApp(root, monitor)

    root.mainloop()
    sys.exit(web_monitor_app.qt_app.exec_())
