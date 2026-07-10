#!/usr/bin/env python3
"""
Headless (offscreen) smoke test for the PySide6/Qt GUI (main.py).

Runs the real Qt event loop against the Demo Simulator: connect -> scan ->
configure a channel -> run a sequence, and checks the simulator state + the
auto-hide visibility filter. Skips cleanly if PySide6 is not installed.

Run:  python src/test_gui.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QTimer
except Exception as e:  # pragma: no cover
    print(f"SKIP: PySide6 not available ({e})")
    raise SystemExit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")

    # Auto-dismiss modal dialogs so the loop never blocks.
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)

    import main
    w = main.LDCMainWindow()
    w.show()
    res = {}

    def start():
        # Both view modes must build without error.
        w._set_view_mode(False)   # Cards
        w._set_view_mode(True)    # Table
        w.com_combo.setCurrentText("Demo Simulator")
        w.connect_serial()
        w.start_scan()
        QTimer.singleShot(200, wait_scan)

    def wait_scan():
        if w.is_scanning:
            QTimer.singleShot(200, wait_scan)
            return
        res['shown'] = w._shown()
        c = w.cards[0]
        c.tec_cmd.setCurrentText("ON")
        c.las_cmd.setCurrentText("ON")
        c.t_target.setText("23.0")
        c.i_target.setText("6.0")
        w.execute_channels([1])
        QTimer.singleShot(200, wait_run)

    def wait_run():
        if w.is_executing:
            QTimer.singleShot(200, wait_run)
            return
        res['tec'] = w.ctl.sim_state['TEC_ON'][0]
        res['las'] = w.ctl.sim_state['LAS_ON'][0]
        res['T'] = round(w.ctl.sim_state['T_actual'][0], 2)
        res['I'] = round(w.ctl.sim_state['I_actual'][0], 2)
        res['status'] = w.cards[0].status.text()
        app.quit()

    QTimer.singleShot(200, start)
    QTimer.singleShot(45000, app.quit)  # safety timeout
    app.exec()
    return res


def main():
    res = run()
    print("shown channels after scan:", res.get('shown'))
    print("ch1 sim: TEC", res.get('tec'), "LAS", res.get('las'),
          "T", res.get('T'), "I", res.get('I'))
    print("ch1 status:", res.get('status'))

    checks = [
        ("scan auto-hid empty/no-laser channels (shown = populated)",
         res.get('shown') == [0, 1, 4]),
        ("TEC + LAS enabled on ch1", res.get('tec') == 1 and res.get('las') == 1),
        ("ramped to target T/I", abs(res.get('T', 0) - 23.0) < 0.1 and abs(res.get('I', 0) - 6.0) < 0.1),
        ("final status set", str(res.get('status', "")).startswith("Final Set:")),
    ]
    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    print("\nQt offscreen smoke test:", "PASSED" if ok else "FAILED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
