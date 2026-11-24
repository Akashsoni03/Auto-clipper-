import os, json, uuid, subprocess, requests
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import yt_dlp
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)
UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
AUDD_API_KEY = os.getenv("AUDD_API_KEY", "")

@app.route("/")
def serve_frontend():
    return send_from_directory("static", "index.html")

@app.route("/outputs/<path:filename>")
def serve_outputs(filename):
    return send_from_directory("outputs", filename)

def get_video_duration(video_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    return float(subprocess.check_output(cmd).decode().strip())

def check_copyright_audio(video_path):
    if not AUDD_API_KEY:
        return {"audio_scan": "Skipped (AUDD_API_KEY set nahi hai)", "detected_music": None}
    sample = f"{video_path}_sample.mp3"
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-ss", "10", "-t", "15", "-q:a", "4", sample], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open(sample, "rb") as f:
            res = requests.post("https://api.audd.io/", data={"api_token": AUDD_API_KEY}, files={"file": f}, timeout=20).json()
        r = res.get("result")
        if r:
            return {"audio_scan": "Copyrighted music detect hua", "detected_music": f"{r.get('title')} - {r.get('artist')}"}
        return {"audio_scan": "Safe (Koi copyrighted song nahi mila)", "detected_music": None}
    except Exception as e:
        return {"audio_scan": f"Check failed: {e}", "detected_music": None}
    finally:
        if os.path.exists(sample): os.remove(sample)

def get_5_best_clips(total_duration, title=""):
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
            prompt = f'Video title "{title}", length {round(total_duration,1)}s. Return ONLY a JSON array of 5 best clips (15-40s each): [{{"start":0.0,"end":25.0,"title":"Title","thumbnail_text":"HOOK"}}]'
            res = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20).json()
            raw = res["candidates"][0]["content"]["parts"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
            clips = json.loads(raw)
            if isinstance(clips, list) and clips: return clips[:5]
        except Exception:
            pass
    part = max(10.0, min(30.0, total_duration / 5))
    return [{"start": round(i*part, 1), "end": round(min((i+1)*part, total_duration), 1), "title": f"Highlight Clip {i+1}", "thumbnail_text": f"BEST MOMENT #{i+1}"} for i in range(5) if i*part < total_duration - 2]

def create_thumbnail(video_path, thumb_path, text, ts):
    subprocess.run(["ffmpeg", "-y", "-ss", str(ts), "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(thumb_path): return
    img = Image.open(thumb_path).convert("RGBA")
    w, h = img.size
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rectangle([(0, int(h*0.72)), (w, h)], fill=(0, 0, 0, 180))
    img = Image.alpha_composite(img, ov).convert("RGB")
    ImageDraw.Draw(img).text((20, int(h*0.80)), text.upper()[:28], fill=(255, 220, 0), font=ImageFont.load_default())
    img.save(thumb_path, "JPEG", quality=90)

@app.route("/api/process", methods=["POST"])
def process_video():
    youtube_url = request.form.get("youtube_url", "").strip()
    video_file = request.files.get("video_file")
    if not youtube_url and not video_file:
        return jsonify({"detail": "YouTube link ya video file dein."}), 400

    job_id = str(uuid.uuid4())[:8]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_video = str(UPLOAD_DIR / f"{job_id}.mp4")
    copyright_info = {"license": "Uploaded Video", "warning": False}
    video_title = "Uploaded Video"

    if youtube_url:
        ydl_opts = {"format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", "outtmpl": input_video, "merge_output_format": "mp4", "quiet": True}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                video_title = info.get("title", "YouTube Video")
                yt_license = info.get("license") or "Standard YouTube License"
                copyright_info["license"] = yt_license
                if "Creative Commons" not in yt_license: copyright_info["warning"] = True
        except Exception as e:
            return jsonify({"detail": f"YouTube error: {e}"}), 400
    else:
        video_file.save(input_video)

    audio_check = check_copyright_audio(input_video)
    copyright_info.update(audio_check)
    if audio_check.get("detected_music"): copyright_info["warning"] = True

    total_duration = get_video_duration(input_video)
    best_clips = get_5_best_clips(total_duration, video_title)

    results = []
    for idx, clip in enumerate(best_clips, start=1):
        start, end = float(clip["start"]), float(clip["end"])
        clip_name, thumb_name = f"clip_{idx}.mp4", f"thumb_{idx}.jpg"
        clip_path, thumb_path = str(job_dir / clip_name), str(job_dir / thumb_name)
        subprocess.run(["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", input_video, "-c", "copy", "-movflags", "+faststart", clip_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        create_thumbnail(input_video, thumb_path, clip.get("thumbnail_text", f"CLIP {idx}"), start + ((end - start) / 2))
        results.append({"clip_number": idx, "title": clip.get("title", f"Clip {idx}"), "start": start, "end": end, "thumbnail_url": f"/outputs/{job_id}/{thumb_name}", "preview_url": f"/outputs/{job_id}/{clip_name}", "download_url": f"/api/download/{job_id}/{clip_name}"})

    return jsonify({"job_id": job_id, "copyright": copyright_info, "clips": results})

@app.route("/api/download/<job_id>/<filename>")
def download_clip(job_id, filename):
    return send_from_directory(OUTPUT_DIR / job_id, filename, as_attachment=True, download_name=f"auto_clip_{job_id}_{filename}")

