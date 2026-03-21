import cv2
import numpy as np
import mss
import os
import keyboard
from datetime import datetime
from collections import deque
import time

# Flagsq
save_last_30s = False
stop_recording = False
keyboard.add_hotkey('q', lambda: globals().__setitem__('stop_recording', True))
keyboard.add_hotkey('~', lambda: globals().__setitem__('save_last_30s', True))

# Folders
save_folder = r"Recordings"
clip_folder = r"Clips"

os.makedirs(save_folder, exist_ok=True)
os.makedirs(clip_folder, exist_ok=True)

fps_nominal = 20
seconds=30

size = fps_nominal * seconds
buffer = deque(maxlen=size)

with mss.mss() as sct:
    monitor = sct.monitors[1] 
    width = monitor["width"]
    height = monitor["height"]

    # File
    live_filename = datetime.now().strftime("screen_live_%Y%m%d_%H%M%S.mp4")
    live_filepath = os.path.join(save_folder, live_filename)
    fourcc = cv2.VideoWriter_fourcc(*"XVID")
    live_out = cv2.VideoWriter(live_filepath, fourcc, fps_nominal, (width, height))

    print("Recording started... Press '~' to clip, Q to stop.")

    prev_time = time.time()

    while True:
        start_time = time.time()
        screenshot = sct.grab(monitor)
        frame = np.array(screenshot)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        # FPS Counter
        elapsed = start_time - prev_time
        prev_time = start_time
        fps_dynamic = 1 / elapsed if elapsed > 0 else 0

        cv2.putText(frame, f"FPS: {fps_dynamic:.2f}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        # Writing frames
        live_out.write(frame)
        buffer.append(frame.copy())

        # Live feed
        cv2.imshow("Live Screen Recording", frame)

        # Clip 30 seconds
        if save_last_30s and len(buffer)==buffer.maxlen:
            save_filename = datetime.now().strftime("clipped_30s_%Y%m%d_%H%M%S.mp4")
            buffer_copy = list(buffer)
            save_path = os.path.join(clip_folder, save_filename)
            out = cv2.VideoWriter(save_path, fourcc, fps_nominal, (width, height))
            for f in buffer_copy:
                out.write(f)
            out.release()
            print("Last 30 seconds clipped")
            save_last_30s = False

        # Stop recording
        if stop_recording:
            print("Stopping recording..")
            break

        # Exit
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    live_out.release()
    cv2.destroyAllWindows()

print(f"Recording saved to: {save_folder}")