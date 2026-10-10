"""FaceAttend Pro - local face-recognition attendance demo.
Uses OpenCV Haar cascades + a NumPy LBP histogram matcher (no deep-learning frameworks).
"""
from __future__ import annotations
import csv, sqlite3, threading, time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import cv2
import numpy as np
from PIL import Image, ImageTk

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "attendance.db"
FACE_DIR = ROOT / "faces"
EXPORT_DIR = ROOT / "exports"
IMG_SIZE = (120, 120)
RECOGNITION_THRESHOLD = 0.48  # Lower is stricter; tune with your own camera/data.
SAMPLES_TO_CAPTURE = 12

for folder in (DB_PATH.parent, FACE_DIR, EXPORT_DIR):
    folder.mkdir(parents=True, exist_ok=True)


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect_db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_no TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            attendance_date TEXT NOT NULL,
            check_in TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'face',
            UNIQUE(student_id, attendance_date)
        );
        """)


def lbp_histogram(gray: np.ndarray) -> np.ndarray:
    """Compute a simple 8-neighbour LBP histogram, normalized to sum to 1."""
    gray = cv2.resize(gray, IMG_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.equalizeHist(gray)
    center = gray[1:-1, 1:-1]
    neighbors = [gray[:-2, :-2], gray[:-2, 1:-1], gray[:-2, 2:],
                 gray[1:-1, 2:], gray[2:, 2:], gray[2:, 1:-1],
                 gray[2:, :-2], gray[1:-1, :-2]]
    code = np.zeros(center.shape, dtype=np.uint8)
    for bit, neighbor in enumerate(neighbors):
        code |= ((neighbor >= center).astype(np.uint8) << bit)
    # Keep coarse spatial information: 4 x 4 cells, 256 bins per cell.
    # This is more discriminative than one histogram for the whole face.
    cell_h, cell_w = code.shape[0] // 4, code.shape[1] // 4
    parts = []
    for row in range(4):
        for col in range(4):
            cell = code[row*cell_h:(row+1)*cell_h, col*cell_w:(col+1)*cell_w]
            hist = np.bincount(cell.ravel(), minlength=256).astype(np.float32)
            hist /= (hist.sum() + 1e-8)
            parts.append(hist)
    return np.concatenate(parts)


def hist_distance(a: np.ndarray, b: np.ndarray) -> float:
    # Average Bhattacharyya-style distance over the 16 spatial cells.
    aa = a.reshape(16, 256)
    bb = b.reshape(16, 256)
    coeff = np.sum(np.sqrt(np.maximum(aa, 0) * np.maximum(bb, 0)), axis=1)
    distances = np.sqrt(np.maximum(0.0, 1.0 - np.minimum(coeff, 1.0)))
    return float(np.mean(distances))


class FaceAttendanceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FaceAttend Pro | Sistem Absensi Otomatis")
        self.root.geometry("1200x780")
        self.root.minsize(1024, 700)
        self.root.configure(bg="#f8fafc")
        
        self.cap = None
        self.camera_running = False
        self.camera_mode = None
        self.after_id = None
        self.current_student_id = None
        self.capture_count = 0
        self.capture_vectors = []
        self.gallery = []
        self.last_marked = {}
        
        self.face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if self.face_cascade.empty():
            messagebox.showerror("Kesalahan", "Haar cascade OpenCV tidak dapat dimuat.")
        
        init_db()
        self._style()
        self._build_ui()
        self.refresh_students()
        self.refresh_attendance()
        self.load_gallery()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _style(self):
        style = ttk.Style()
        try: style.theme_use("clam")
        except tk.TclError: pass
        
        # Color Palette: Modern Slate & Indigo
        style.configure(".", background="#f8fafc", foreground="#1e293b", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f8fafc")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("TLabel", background="#f8fafc", foreground="#1e293b", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background="#ffffff", foreground="#1e293b", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"), foreground="#0f172a", background="#f8fafc")
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#64748b", background="#f8fafc")
        style.configure("Heading.TLabel", font=("Segoe UI", 12, "bold"), foreground="#334155", background="#ffffff")
        
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=8, relief="flat")
        style.configure("Accent.TButton", background="#4f46e5", foreground="white")
        style.map("Accent.TButton", background=[("active", "#4338ca"), ("pressed", "#3730a3")])
        
        style.configure("Danger.TButton", background="#ef4444", foreground="white")
        style.map("Danger.TButton", background=[("active", "#dc2626")])

        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10), background="#ffffff", fieldbackground="#ffffff", bordercolor="#e2e8f0")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), background="#f1f5f9", foreground="#334155", relief="flat")

    def _build_ui(self):
        # Header Utama
        header = ttk.Frame(self.root, padding=(24, 16, 24, 12))
        header.pack(fill="x")
        
        title_frame = ttk.Frame(header)
        title_frame.pack(side="left", fill="x", expand=True)
        ttk.Label(title_frame, text="⚡ FaceAttend Pro", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_frame, text="Sistem Absensi Cerdas Berbasis Pengenalan Wajah (OpenCV + NumPy + SQLite)", style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        # Badge / Status Header Kanan
        self.header_badge = ttk.Label(header, text=" ● Sistem Siap ", font=("Segoe UI", 9, "bold"), foreground="#16a34a", background="#dcfce7", padding=(10, 6))
        self.header_badge.pack(side="right", padx=4)

        # Notebook Tabs
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.tab_att = ttk.Frame(self.tabs, padding=16)
        self.tab_students = ttk.Frame(self.tabs, padding=16)
        self.tab_report = ttk.Frame(self.tabs, padding=16)
        
        self.tabs.add(self.tab_att, text="  📸 Panel Absensi  ")
        self.tabs.add(self.tab_students, text="  👥 Kelola Mahasiswa  ")
        self.tabs.add(self.tab_report, text="  📊 Laporan & Rekap  ")
        
        self._build_attendance_tab()
        self._build_students_tab()
        self._build_report_tab()

        # Footer Status Bar
        self.status = tk.StringVar(value="Sistem siap. Silakan daftarkan mahasiswa atau mulai absensi.")
        status_bar = ttk.Frame(self.root, padding=(12, 6))
        status_bar.pack(fill="x", side="bottom")
        ttk.Label(status_bar, textvariable=self.status, font=("Segoe UI", 9), foreground="#475569").pack(side="left")

    def _build_attendance_tab(self):
        main = ttk.Frame(self.tab_att)
        main.pack(fill="both", expand=True)
        
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True, padx=(0, 16))
        
        # Kamera View Container
        cam_frame = ttk.Frame(left, style="Card.TFrame")
        cam_frame.pack(fill="both", expand=True)
        
        self.video_label = ttk.Label(cam_frame, text="Kamera belum aktif\nKlik 'Mulai Absensi' untuk memindai wajah", anchor="center", background="#e2e8f0", foreground="#64748b", font=("Segoe UI", 11))
        self.video_label.pack(fill="both", expand=True, padx=2, pady=2)
        
        # Tombol Kontrol Kamera
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="▶ Mulai Absensi", style="Accent.TButton", command=self.start_attendance).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="⏹ Hentikan Kamera", command=self.stop_camera).pack(side="left")
        
        # Panel Panduan Kanan
        right = ttk.Frame(main, width=300)
        right.pack(side="right", fill="y")
        
        guide_card = ttk.Frame(right, style="Card.TFrame", padding=16)
        guide_card.pack(fill="both", expand=True)
        
        ttk.Label(guide_card, text="💡 Panduan Penggunaan", style="Heading.TLabel").pack(anchor="w", pady=(0, 10))
        
        steps = [
            "1. Buka tab **Kelola Mahasiswa** untuk mendaftarkan NIM & Nama.",
            "2. Rekam minimal **8–12 sampel wajah** dengan pencahayaan yang cukup.",
            "3. Kembali ke tab **Absensi** dan klik **Mulai Absensi**.",
            "4. Posisikan wajah di depan kamera secara jelas.",
            "5. Sistem mencatat otomatis kehadiran **1 kali per hari** per mahasiswa."
        ]
        for step in steps:
            ttk.Label(guide_card, text=step, wraplength=260, justify="left", font=("Segoe UI", 9)).pack(anchor="w", pady=4)
        
        ttk.Separator(guide_card, orient="horizontal").pack(fill="x", pady=14)
        ttk.Label(guide_card, text="🔒 Catatan Privasi", font=("Segoe UI", 10, "bold"), foreground="#334155").pack(anchor="w", pady=(0, 4))
        ttk.Label(guide_card, text="Data foto wajah disimpan sepenuhnya secara lokal di dalam folder faces/. Aman dan privat.", wraplength=260, justify="left", font=("Segoe UI", 9), foreground="#64748b").pack(anchor="w")

    def _build_students_tab(self):
        # Form Tambah Mahasiswa
        form = ttk.LabelFrame(self.tab_students, text=" Registrasi Mahasiswa Baru ", padding=16)
        form.pack(fill="x", pady=(0, 14))
        
        ttk.Label(form, text="NIM / ID", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        self.student_no_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.student_no_var, width=22, font=("Segoe UI", 10)).grid(row=0, column=1, sticky="ew", padx=(0, 24), pady=6)
        
        ttk.Label(form, text="Nama Lengkap", font=("Segoe UI", 9, "bold")).grid(row=0, column=2, sticky="w", padx=(0, 8), pady=6)
        self.student_name_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.student_name_var, width=32, font=("Segoe UI", 10)).grid(row=0, column=3, sticky="ew", pady=6)
        
        ttk.Button(form, text="✨ Simpan & Rekam Wajah", style="Accent.TButton", command=self.add_student).grid(row=1, column=1, sticky="w", pady=(8, 0))
        form.columnconfigure(3, weight=1)
        
        # Header Tabel Mahasiswa & Statistik
        table_top = ttk.Frame(self.tab_students)
        table_top.pack(fill="x", pady=(0, 6))
        ttk.Label(table_top, text="Daftar Mahasiswa Terdaftar", font=("Segoe UI", 11, "bold")).pack(side="left")
        
        self.lbl_total_students = ttk.Label(table_top, text="Total: 0 mahasiswa", font=("Segoe UI", 9, "bold"), foreground="#4f46e5")
        self.lbl_total_students.pack(side="right")

        # Treeview Mahasiswa
        cols = ("student_no", "name", "samples", "created")
        self.student_tree = ttk.Treeview(self.tab_students, columns=cols, show="headings", height=12)
        for col, label, width in [("student_no", "NIM / ID", 160), ("name", "Nama Lengkap", 340), ("samples", "Jumlah Sampel Wajah", 160), ("created", "Tanggal Terdaftar", 200)]:
            self.student_tree.heading(col, text=label)
            self.student_tree.column(col, width=width, anchor="w")
        self.student_tree.pack(fill="both", expand=True, pady=(2, 10))
        
        # Tombol Aksi Tabel
        bottom = ttk.Frame(self.tab_students)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="📷 Tambah/Perbarui Sampel Wajah", command=self.add_samples_selected).pack(side="left")
        ttk.Button(bottom, text="🗑 Hapus Mahasiswa", style="Danger.TButton", command=self.delete_student).pack(side="left", padx=10)
        ttk.Button(bottom, text="🔄 Muat Ulang", command=self.refresh_students).pack(side="right")

    def _build_report_tab(self):
        top = ttk.Frame(self.tab_report, padding=(0, 0, 0, 10))
        top.pack(fill="x")
        
        ttk.Label(top, text="Pilih Tanggal (YYYY-MM-DD):", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        self.report_date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Entry(top, textvariable=self.report_date_var, width=16, font=("Segoe UI", 10)).pack(side="left", padx=(0, 10))
        
        ttk.Button(top, text="🔍 Tampilkan", command=self.refresh_attendance).pack(side="left")
        ttk.Button(top, text="📥 Ekspor ke CSV", style="Accent.TButton", command=self.export_csv).pack(side="right")
        
        # Treeview Laporan Kehadiran
        cols = ("date", "time", "student_no", "name", "method")
        self.att_tree = ttk.Treeview(self.tab_report, columns=cols, show="headings", height=16)
        for col, label, width in [("date", "Tanggal", 140), ("time", "Jam Masuk", 130), ("student_no", "NIM / ID", 160), ("name", "Nama Lengkap", 360), ("method", "Metode Verifikasi", 140)]:
            self.att_tree.heading(col, text=label)
            self.att_tree.column(col, width=width, anchor="w")
        self.att_tree.pack(fill="both", expand=True, pady=(4, 0))

    def add_student(self):
        no = self.student_no_var.get().strip()
        name = self.student_name_var.get().strip()
        if not no or not name:
            messagebox.showwarning("Data belum lengkap", "Harap isi NIM/ID dan Nama Lengkap terlebih dahulu.")
            return
        try:
            with connect_db() as con:
                cur = con.execute("INSERT INTO students(student_no,name,created_at) VALUES(?,?,?)", (no, name, datetime.now().isoformat(timespec="seconds")))
                sid = cur.lastrowid
        except sqlite3.IntegrityError:
            messagebox.showerror("NIM sudah terdaftar", "NIM/ID tersebut sudah ada di database. Gunakan NIM yang berbeda.")
            return
        
        (FACE_DIR / str(sid)).mkdir(parents=True, exist_ok=True)
        self.student_no_var.set("")
        self.student_name_var.set("")
        self.refresh_students()
        self.current_student_id = sid
        self.capture_count = 0
        self.capture_vectors = []
        self.start_capture(sid)

    def selected_student_id(self):
        selection = self.student_tree.selection()
        if not selection: return None
        return int(self.student_tree.item(selection[0], "tags")[0])

    def add_samples_selected(self):
        sid = self.selected_student_id()
        if sid is None:
            messagebox.showinfo("Pilih mahasiswa", "Pilih salah satu mahasiswa dari daftar terlebih dahulu.")
            return
        self.start_capture(sid)

    def start_capture(self, sid):
        self.current_student_id = sid
        self.capture_count = 0
        self.capture_vectors = []
        if not self.open_camera("capture"):
            return
        self.header_badge.config(text=" ● Mode Rekam Sampel ", foreground="#d97706", background="#fef3c7")
        self.status.set(f"Pengambilan sampel wajah aktif: hadapkan wajah ke kamera ({SAMPLES_TO_CAPTURE} sampel).")

    def open_camera(self, mode):
        if self.camera_running: self.stop_camera()
        self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap.release()
            self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            self.cap = None
            messagebox.showerror("Kamera tidak tersedia", "Tidak dapat membuka kamera. Pastikan webcam terhubung dan tidak digunakan aplikasi lain.")
            return False
        self.camera_mode = mode
        self.camera_running = True
        self._camera_tick()
        return True

    def start_attendance(self):
        if not self.gallery: self.load_gallery()
        if not self.gallery:
            messagebox.showwarning("Belum ada sampel", "Belum ada sampel wajah terdaftar. Daftarkan mahasiswa dan ambil sampel terlebih dahulu.")
            return
        if self.open_camera("attendance"):
            self.header_badge.config(text=" ● Pemindaian Absensi Aktif ", foreground="#16a34a", background="#dcfce7")
            self.status.set("Absensi aktif. Wajah yang dikenali akan tercatat otomatis.")

    def stop_camera(self):
        self.camera_running = False
        if self.after_id:
            try: self.root.after_cancel(self.after_id)
            except tk.TclError: pass
            self.after_id = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.camera_mode = None
        self.header_badge.config(text=" ● Sistem Siap ", foreground="#16a34a", background="#dcfce7")
        self.video_label.configure(image="", text="Kamera dihentikan\nKlik 'Mulai Absensi' untuk memindai kembali", background="#e2e8f0", foreground="#64748b")
        self.video_label.image = None

    def _camera_tick(self):
        if not self.camera_running or self.cap is None: return
        ok, frame = self.cap.read()
        if not ok:
            self.status.set("Gagal membaca frame dari kamera.")
            self.after_id = self.root.after(100, self._camera_tick)
            return
        
        frame = cv2.flip(frame, 1)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self.face_cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(70, 70))
        
        for (x, y, w, h) in faces:
            face = gray[y:y+h, x:x+w]
            label = "Wajah terdeteksi"
            color = (30, 180, 80)
            
            if self.camera_mode == "capture":
                now = time.monotonic()
                if not hasattr(self, "last_capture_time"): self.last_capture_time = 0
                if now - self.last_capture_time >= 0.35 and self.capture_count < SAMPLES_TO_CAPTURE:
                    normalized = cv2.resize(face, IMG_SIZE)
                    normalized = cv2.equalizeHist(normalized)
                    path = FACE_DIR / str(self.current_student_id) / f"sample_{int(time.time()*1000)}.png"
                    cv2.imwrite(str(path), normalized)
                    self.capture_count += 1
                    self.last_capture_time = now
                
                label = f"Rekam Sampel: {self.capture_count}/{SAMPLES_TO_CAPTURE}"
                color = (0, 140, 255)
                
                if self.capture_count >= SAMPLES_TO_CAPTURE:
                    self.camera_running = False
                    if self.cap: self.cap.release(); self.cap = None
                    self.camera_mode = None
                    self.load_gallery()
                    self.refresh_students()
                    self.header_badge.config(text=" ● Sistem Siap ", foreground="#16a34a", background="#dcfce7")
                    self.status.set("Pengambilan sampel wajah selesai.")
                    messagebox.showinfo("Berhasil", f"{SAMPLES_TO_CAPTURE} sampel wajah berhasil disimpan.")
                    self.video_label.configure(image="", text="Sampel berhasil direkam", background="#e2e8f0", foreground="#64748b")
                    self.video_label.image = None
                    return
            
            elif self.camera_mode == "attendance":
                recognized = self.recognize(face)
                if recognized:
                    sid, name, score = recognized
                    label = f"{name} ({score:.2f})"
                    color = (0, 200, 80)
                    self.mark_attendance(sid)
                else:
                    label = "Tidak Dikenali"
                    color = (0, 0, 230)
            
            cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)
            cv2.putText(frame, label, (x, max(24, y-10)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
            
        if self.camera_mode == "capture":
            cv2.putText(frame, f"Progress: {self.capture_count}/{SAMPLES_TO_CAPTURE}", (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        im = Image.fromarray(rgb)
        im.thumbnail((760, 500))
        photo = ImageTk.PhotoImage(im)
        self.video_label.configure(image=photo, text="", background="#000000")
        self.video_label.image = photo
        self.after_id = self.root.after(25, self._camera_tick)

    def load_gallery(self):
        gallery = []
        with connect_db() as con:
            students = con.execute("SELECT id,name FROM students").fetchall()
        for student in students:
            folder = FACE_DIR / str(student["id"])
            if not folder.exists(): continue
            for path in folder.glob("*.png"):
                image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if image is not None:
                    gallery.append((int(student["id"]), student["name"], lbp_histogram(image)))
        self.gallery = gallery

    def recognize(self, face):
        if not self.gallery: return None
        query = lbp_histogram(face)
        scored = [(hist_distance(query, hist), sid, name) for sid, name, hist in self.gallery]
        scored.sort(key=lambda item: item[0])
        best = scored[0]
        if best[0] <= RECOGNITION_THRESHOLD:
            return best[1], best[2], best[0]
        return None

    def mark_attendance(self, sid):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.last_marked.get(sid) == today: return
        try:
            with connect_db() as con:
                con.execute("INSERT INTO attendance(student_id,attendance_date,check_in,method) VALUES(?,?,?,?)", (sid, today, now.strftime("%H:%M:%S"), "face/LBP"))
            self.last_marked[sid] = today
            self.status.set(f"Absensi berhasil dicatat pada pukul {now.strftime('%H:%M:%S')}.")
            self.refresh_attendance()
        except sqlite3.IntegrityError:
            self.last_marked[sid] = today

    def refresh_students(self):
        if not hasattr(self, "student_tree"): return
        for item in self.student_tree.get_children(): self.student_tree.delete(item)
        with connect_db() as con:
            rows = con.execute("SELECT * FROM students ORDER BY name").fetchall()
        for row in rows:
            count = len(list((FACE_DIR / str(row["id"])).glob("*.png"))) if (FACE_DIR / str(row["id"])).exists() else 0
            self.student_tree.insert("", "end", values=(row["student_no"], row["name"], f"{count} sampel", row["created_at"][:10]), tags=(str(row["id"]),))
        if hasattr(self, "lbl_total_students"):
            self.lbl_total_students.config(text=f"Total: {len(rows)} mahasiswa")

    def refresh_attendance(self):
        if not hasattr(self, "att_tree"): return
        date = self.report_date_var.get().strip() if hasattr(self, "report_date_var") else datetime.now().strftime("%Y-%m-%d")
        try: datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("Format tanggal salah", "Gunakan format YYYY-MM-DD.")
            return
        for item in self.att_tree.get_children(): self.att_tree.delete(item)
        with connect_db() as con:
            rows = con.execute("""SELECT a.attendance_date,a.check_in,s.student_no,s.name,a.method
                FROM attendance a JOIN students s ON s.id=a.student_id
                WHERE a.attendance_date=? ORDER BY a.check_in""", (date,)).fetchall()
        for row in rows: 
            self.att_tree.insert("", "end", values=(row["attendance_date"], row["check_in"], row["student_no"], row["name"], row["method"]))

    def delete_student(self):
        sid = self.selected_student_id()
        if sid is None:
            messagebox.showinfo("Pilih mahasiswa", "Pilih mahasiswa yang ingin dihapus dari tabel.")
            return
        if not messagebox.askyesno("Konfirmasi Hapus", "Hapus mahasiswa ini beserta seluruh data absensi dan sampel wajahnya?"):
            return
        with connect_db() as con: 
            con.execute("DELETE FROM students WHERE id=?", (sid,))
        folder = FACE_DIR / str(sid)
        if folder.exists():
            for file in folder.glob("*"):
                try: file.unlink()
                except OSError: pass
            try: folder.rmdir()
            except OSError: pass
        self.load_gallery()
        self.refresh_students()
        self.refresh_attendance()
        self.status.set("Data mahasiswa berhasil dihapus.")

    def export_csv(self):
        date = self.report_date_var.get().strip()
        try: datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("Format tanggal salah", "Gunakan format YYYY-MM-DD.")
            return
        default = EXPORT_DIR / f"absensi_{date}.csv"
        path = filedialog.asksaveasfilename(title="Simpan Laporan CSV", initialfile=default.name, initialdir=str(EXPORT_DIR), defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path: return
        with connect_db() as con:
            rows = con.execute("""SELECT a.attendance_date,a.check_in,s.student_no,s.name,a.method
                FROM attendance a JOIN students s ON s.id=a.student_id
                WHERE a.attendance_date=? ORDER BY a.check_in""", (date,)).fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Tanggal", "Jam Masuk", "NIM/ID", "Nama Lengkap", "Metode"])
            writer.writerows([[r["attendance_date"], r["check_in"], r["student_no"], r["name"], r["method"]] for r in rows])
        messagebox.showinfo("Ekspor Berhasil", f"Laporan berhasil disimpan di:\n{path}")

    def on_close(self):
        self.stop_camera()
        self.root.destroy()


def main():
    root = tk.Tk()
    FaceAttendanceApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()