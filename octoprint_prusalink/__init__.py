# coding=utf-8
# octoprint_prusalink/__init__.py

import octoprint.plugin
from octoprint.printer import PrinterInterface
from octoprint.util.files import unix_timestamp_to_m20_timestamp
from octoprint.settings import settings
from octoprint.filemanager.analysis import AbstractAnalysisQueue
from requests.auth import HTTPDigestAuth
import requests
import logging
import re
import json
import time
import threading

class MyCustomGcodeAnalysisQueue(AbstractAnalysisQueue):

    def __init__(self):
        super().__init__()
        self._logger = logging.getLogger("octoprint.plugins.prusalink")

    def enqueue(self, entry, high_priority=False):
        self._logger.info(f"PrusaLink analyse")
        return super().enqueue(entry, high_priority)

class PrusaLinkPlugin(octoprint.plugin.StartupPlugin,
                                 octoprint.plugin.TemplatePlugin,
                                 octoprint.plugin.SettingsPlugin,
                                 octoprint.plugin.AssetPlugin):

    def __init__(self):
        super().__init__()
        self._thread = None
        self._printer_state = None
        self._stop_event = threading.Event()

        self._logger = logging.getLogger("octoprint.plugins.prusalink")
        self.prusalink_host = self.get_settings_defaults()["host"]
        self.prusalink_user = self.get_settings_defaults()["username"]
        self.prusalink_pass = self.get_settings_defaults()["password"]
        self.auth = HTTPDigestAuth(self.prusalink_user, self.prusalink_pass)


    def start_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self.printer_status_func, daemon=True)
            self._thread.start()

    def stop_thread(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join()

    def on_after_startup(self):
        self._logger.info("PrusaLink started")
        self.start_thread()

        # Prepare Digest Auth credentials
        #self.prusalink_host = self._settings.get(["host"])
        #self.prusalink_user = self._settings.get(["username"])
        #self.prusalink_pass = self._settings.get(["password"])
        #self.prusalink_host = self.get_settings_defaults()["host"]
        #self.prusalink_user = self.get_settings_defaults()["username"]
        #self.prusalink_pass = self.get_settings_defaults()["password"]
        #self.auth = HTTPDigestAuth(self.prusalink_user, self.prusalink_pass)

    def on_shutdown(self):
        self.stop_thread()

    def handle_gcode(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        gcode = gcode.strip().upper()

        self._logger.info(f"PrusaLink gcode:{gcode} / {cmd}")

        if gcode == "M21":  # init sd
            settings().setBoolean( ["serial", "capabilities", "extended_m20"], True)
            return "M118 E1 _m21_SD card ok"

        if gcode == "M20":  # List files
            m20_cmd = cmd.strip().upper()
            file_list = self.get_file_list("L" in m20_cmd, "T" in m20_cmd)
            file_list = "M118 E1 _m20_Begin file list\n" + ''.join([f'M118 E1 _m20_{item}\n' for item in file_list]) + "\nM118 E1 _m20_End file list"
            return (file_list,)

        if gcode == "M23":  # Select file
            filename = cmd.split(" ", 1)[1] if " " in cmd else None
            self._logger.info(f"Selected file: {filename}")
            self.selected_file = filename
            return (f"M118 E1 _m23_Now fresh file: {filename}\nM118 E1 _m23_File opened: {filename} Size: 1\nM118 E1 _m23_File selected",)

        if gcode == "M24":  # Start print
            if hasattr(self, "selected_file"):
                self.start_print(self.selected_file)
                return ("M118 E1 _m24_Print started via PrusaLink",)
            else:
                return ("M118 E1 _m24_No file selected",)

        #elif gcode == "M27":  # Status
        #    progress = self.get_print_progress()
        #    return ("M27",)

        if gcode == "M30":  # Delete file
            filename = cmd.split(" ", 1)[1] if " " in cmd else None
            self._logger.info(f"Deleting file: {filename}")
            self.delete_file(filename)
            return ("M118 E1 _m30_File {filename} deleted",)

        if gcode == "M25":  # abort
            self.abort_print()
            return "M118 E1 _m25_aborted"

        return None

    def get_file_list(self, longNames=False, times=False):
        try:
            longNames=True
            times=True
            url = f"http://{self.prusalink_host}/api/v1/files/usb"
            response = requests.get(url, auth=self.auth)
            response.raise_for_status()
            files = response.json().get("children", [])
            ret = []
            for f in files:
                if f["type"] != "PRINT_FILE":
                    continue
                item = f["name"]
                if times or longNames: item += " 1" # size not supported
                if times: item += " " + unix_timestamp_to_m20_timestamp(f["m_timestamp"])
                if longNames: item += " " + f["display_name"]
                ret.append(item)

            return ret
        except Exception as e:
            self._logger.error(f"Error getting file list: {e}")
            return ["example.gcode"]

    def start_print(self, filename):
        try:
            url = f"http://{self.prusalink_host}/api/files/usb{filename}"
            self._logger.info(f"gcode url: {url}")

            payload = {'command': 'start'}
            response = requests.post(url, auth=self.auth, data=json.dumps(payload))
            response.raise_for_status()
        except Exception as e:
            self._logger.error(f"Error starting print: {e}")

    def abort_print(self):
        try:
            url = f"http://{self.prusalink_host}/api/v1/job"
            job_info = requests.get(url, auth=self.auth)

            if "{" in job_info.text and "id" in job_info.json():
                id = str(job_info.json()['id'])
                url = f"http://{self.prusalink_host}/api/v1/job/"+ str(id)
                response = requests.delete(url, auth=self.auth)
                response.raise_for_status()
        except Exception as e:
            self._logger.error(f"Error abort: {e}")

        return "M118 E1 _m25_aborted"

    def delete_file(self, filename):
        try:
            url = f"http://{self.prusalink_host}/api/files/usb/{filename}"
            response = requests.delete(url, auth=self.auth)
            response.raise_for_status()
        except Exception as e:
            self._logger.error(f"Error deleting file: {e}")

    def get_print_progress(self):
        try:
            url = f"http://{self.prusalink_host}/api/job"
            response = requests.get(url, auth=self.auth)
            response.raise_for_status()
            return response.json().get("progress", {}).get("completion", 0.0)
        except Exception as e:
            self._logger.error(f"Error getting print progress: {e}")
            return 0.0

    ## Required plugin hooks and metadata
    def get_settings_defaults(self):
        return dict(
            host="your host ip",
            username="your user name",
            password="your password"
        )

    def get_settings_version(self):
        return 1

    def get_template_configs(self):
        return [dict(type="settings", custom_bindings=False)]

    def get_assets(self):
        return dict(js=[], css=[], less=[])
        return dict(
            js=["js/prusalink.js"],
            css=["css/prusalink.css"],
            less=["less/prusalink.less"]
        )


    def get_update_information(self):
        return dict(
            prusalink_sdemulator=dict(
                displayName="PrusaLink",
                version="0.1.0",
                type="github_release",
                user="yourusername",
                repo="OctoPrint-PrusaLink",
                current="0.1.0"
            )
        )

    def sd_upload(self,printer, filename, path, sd_upload_started, sd_upload_succeeded, sd_upload_failed, *args, **kwargs):
        self._logger.info("PrusaLink Attempt sd upload: "  + filename + " " + path)

        remote_name = filename  #printer._get_free_remote_name(filename)
        logger.info("Starting dummy SDCard upload from {} to {}".format(filename, remote_name))

        sd_upload_started(filename, remote_name)

        def process():
            self.logger.info("PrusaLink upload started!")
            url = f"http://{self.prusalink_host}/api/v1/files/usb/{remote_name}"
            headers = {'Overwrite':"?1"}
            r = requests.put(url, headers=headers, auth=self.auth, data=open(f"{path}/{filename}", 'rb') )
            self.logger.info("PrusaLink upload done!")
            sd_upload_succeeded(filename, remote_name, 30)

        thread = threading.Thread(target=process, deamon=True)
        thread.start()

        return remote_name

    def save_to_sd(self, path, file_object, links=None, printer_profile=None, allow_overwrite=True, *args, **kwargs):
        self._logger.info("PrusaLink Attempt upload: "  + file_object.filename + " " + path)
        try:
            url = f"http://{self.prusalink_host}/api/v1/files/usb/{file_object.filename}"
            headers = {}
            headers['Overwrite'] = "?1"
            response = requests.put(url, headers=headers, auth=self.auth, data=file_object.stream() )
            response.raise_for_status()

            self._logger.info("Starting Print Job")
            self._printer.select_file(path + " " + file_object.filename, False)
        except Exception as e:
            self._logger.error(f"Error deleting file: {e}")

        return file_object

    def handle_received(self,comm_instance, line, *args, **kwargs):
        #self._logger.info(line)
        line = re.sub(r'^.*?_m\d+_', '', line)
        return line


    def printer_status_func(self):
        url = f"http://{self.prusalink_host}/api/printer"
        while not self._stop_event.is_set():
            try:
                response = requests.get(url, auth=self.auth)
                response.raise_for_status()
                self._printer_state = response.json()
                #self._logger.info(self._printer_state)
            except Exception as e:
                self._printer_state = None
                time.sleep(25)
            time.sleep(5)

    def custom_gcode_analysis_queue(*args, **kwargs):
        logger = logging.getLogger("octoprint.plugins.prusalink")
        logger.info("PrusaLink analyse")
        return dict(gcode=MyCustomGcodeAnalysisQueue)

__plugin_name__ = "PrusaLink"
__plugin_version__ = "0.1.0"
__plugin_pythoncompat__ = ">=3,<4"
__plugin_implementation__ = PrusaLinkPlugin()
__plugin_hooks__ = {
    "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.handle_gcode,
    "octoprint.comm.protocol.gcode.received": __plugin_implementation__.handle_received,
    "octoprint.filemanager.preprocessor"   : __plugin_implementation__.save_to_sd,
    "octoprint.printer.sdcardupload"       : __plugin_implementation__.sd_upload
}
#    "octoprint.filemanager.analysis.factory" : __plugin_implementation__.custom_gcode_analysis_queue

