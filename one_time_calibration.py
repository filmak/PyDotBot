import re
import numpy as np
import pandas as pd

# patterns
info_raw_pattern = re.compile(r'level=info .* event=lh2-raw .*')
calibrating_pattern = re.compile(r'level=info .* event=Calibrating .*')
kv_pattern = re.compile(r'(\w+)=("[^"]*"|\S+)')

def parse_log_file(filename):
    results = []

    with open(filename, "r") as f:
        for line in f:
            if info_raw_pattern.search(line):
                fields = dict((k, v.strip('"')) for k, v in kv_pattern.findall(line))
                entry = {
                    "type": "lh2-raw",
                    "source": fields.get("source"),
                    "polynomial": fields.get("polynomial"),
                    "count1": fields.get("count1"),
                    "count2": fields.get("count2"),
                }
                results.append(entry)

            elif calibrating_pattern.search(line):
                fields = dict((k, v.strip('"')) for k, v in kv_pattern.findall(line))
                points_str = fields.get("points")

                if points_str:
                    # clean up string into a flat list of floats
                    points = np.fromstring(points_str.replace("\n", " "), sep=" ")

                    # we know the first 8 floats are the first 4 points (2D each)
                    points = points[:8].reshape(4, 2).T  # shape (2, 4)

                    entry = {
                        "type": "calibration",
                        "points": points,
                    }
                    results.append(entry)

    return results

def to_numpy_array(df):
    # Get unique sources and assign them indices
    sources = df["source"].unique()
    source_to_idx = {src: i for i, src in enumerate(sources)}
    N = len(sources)

    # Initialize array with NaN (so missing entries are visible)
    arr = np.full((16, N, 2), np.nan)

    # Fill the array
    for _, row in df.iterrows():
        m = int(row["entry"])                 # entry index 0–15
        n = source_to_idx[row["source"]]      # source index
        arr[m, n, 0] = row["count1"]
        arr[m, n, 1] = row["count2"]

    return arr, sources

if __name__ == "__main__":
    parsed = parse_log_file("dotbot.log")
    for r in parsed:
        if r["type"] == "calibration":
            print("Calibration points:\n", r["points"])

    df_lh2 = process_lh2_raw(parsed)
    #print(df_lh2.head())   # or save it, analyze it, etc.

    cam_pts,sources = to_numpy_array(df_lh2)

    