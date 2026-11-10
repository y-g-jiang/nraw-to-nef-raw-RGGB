import os
import sys
from pathlib import Path

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR))


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        from batch import worker
        return worker(sys.argv[2])
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import concurrent.futures
    from dataclasses import dataclass, field
    import json
    import queue
    import subprocess
    import threading
    import time
    import uuid
    from batch import VERSION, SDK_SHA256, atomic_json, canonical, digest, frame_count, output_folders, selection
    from runtime_paths import runtime_files

    @dataclass
    class Item:
        source: Path
        key: str = field(default_factory=lambda: uuid.uuid4().hex)
        template: str = ''
        count: int = 0
        done: int = 0
        total: int = 0
        status: str = '读取中'
        error: str = ''
        output: str = ''

    class App:
        def __init__(self, root):
            self.root = root
            self.data = APP_DIR / 'data'
            self.config_path = self.data / 'settings.json'
            try:
                config = json.loads(self.config_path.read_text(encoding='utf-8'))
                if not isinstance(config, dict):
                    config = {}
            except (OSError, ValueError):
                config = {}
            self.items = {}
            self.messages = queue.Queue()
            self.probes = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            self.busy = False
            self.closing = False
            self.destroyed = threading.Event()
            self.stop_event = threading.Event()
            self.child = None
            self.active_job = None
            self.current = None
            self.error_count = 0
            self.scans = 0
            self.started_at = 0
            self.first = tk.StringVar(value='1')
            self.last = tk.StringVar(value='')
            self.step = tk.StringVar(value='1')
            self.destination = tk.StringVar(value=config.get('destination', str(APP_DIR / 'NEF')))
            self.template = tk.StringVar(value=config.get('template', '') if config.get('optional_template') else '')
            self.threads = tk.StringVar(value=str(config.get('threads', 1)))
            self.summary = tk.StringVar(value='0 个视频')
            self.detail = tk.StringVar(value='')
            self.mutable = []
            root.title('NRAW to NEF')
            icon = RESOURCE_DIR / 'app.ico'
            if icon.is_file():
                root.iconbitmap(str(icon))
            root.geometry('1080x730')
            root.minsize(900, 640)
            root.configure(bg='#f5f6f8')
            root.protocol('WM_DELETE_WINDOW', self.close)
            self.style()
            self.build()
            self.template.trace_add('write', lambda *_: self.refresh_defaults())
            self.tree.bind('<Delete>', lambda _event: self.remove())
            root.bind('<Control-o>', lambda _event: self.add_files())
            root.after(100, self.pump)
            saved_queue = config.get('queue', [])
            if isinstance(saved_queue, list):
                self.add_paths([row['source'] for row in saved_queue[:10000] if isinstance(row, dict) and isinstance(row.get('source'), str)])
                templates = {canonical(row['source']): row.get('template', '') for row in saved_queue[:10000]
                             if isinstance(row, dict) and isinstance(row.get('source'), str)}
                for item in self.items.values():
                    item.template = templates.get(canonical(item.source), '') if config.get('optional_template') else ''
                    self.update_row(item)
            for variable, key, fallback in ((self.first, 'first', '1'), (self.last, 'last', ''), (self.step, 'step', '1')):
                variable.set(str(config.get(key, fallback)))

        def style(self):
            style = ttk.Style()
            style.theme_use('clam')
            style.configure('.', font=('Microsoft YaHei UI', 10), background='#f5f6f8', foreground='#202630')
            style.configure('TFrame', background='#f5f6f8')
            style.configure('TLabel', background='#f5f6f8')
            style.configure('Title.TLabel', font=('Segoe UI Semibold', 22), foreground='#172235')
            style.configure('Muted.TLabel', foreground='#798291', font=('Microsoft YaHei UI', 9))
            style.configure('TButton', padding=(14, 8), background='#ffffff', bordercolor='#dce0e6', relief='flat')
            style.map('TButton', background=[('pressed', '#e4e8ed'), ('active', '#eef1f5')], foreground=[('disabled', '#a7adb7')])
            style.configure('Primary.TButton', background='#2563eb', foreground='white', bordercolor='#2563eb', padding=(28, 10))
            style.map('Primary.TButton', background=[('disabled', '#a8bde8'), ('pressed', '#1d4ed8'), ('active', '#3473f1')], foreground=[('disabled', '#f6f8ff'), ('!disabled', '#ffffff')])
            style.configure('TEntry', padding=7, fieldbackground='#ffffff', bordercolor='#dce0e6')
            style.configure('TSpinbox', padding=6, fieldbackground='#ffffff', bordercolor='#dce0e6')
            style.configure('Treeview', rowheight=35, fieldbackground='#ffffff', background='#ffffff', foreground='#263042', borderwidth=0, font=('Microsoft YaHei UI', 10))
            style.configure('Treeview.Heading', background='#edf0f4', foreground='#677284', padding=(10, 10), font=('Microsoft YaHei UI', 9), relief='flat')
            style.map('Treeview', background=[('selected', '#e5edff')], foreground=[('selected', '#174bb4')])
            style.map('Treeview.Heading', background=[('active', '#e7ebf1')])
            style.configure('Horizontal.TProgressbar', troughcolor='#e2e7ef', background='#2563eb', borderwidth=0, thickness=7)

        def button(self, parent, text, command, mutable=True, **options):
            button = ttk.Button(parent, text=text, command=command, **options)
            if mutable:
                self.mutable.append(button)
            return button

        def build(self):
            area = ttk.Frame(self.root, padding=26)
            area.pack(fill='both', expand=True)
            area.columnconfigure(0, weight=1)
            area.rowconfigure(3, weight=1)
            header = ttk.Frame(area)
            header.grid(row=0, column=0, sticky='ew', pady=(0, 22))
            ttk.Label(header, text='NRAW → NEF', style='Title.TLabel').pack(side='left')
            ttk.Label(header, text=VERSION, style='Muted.TLabel').pack(side='left', padx=14, pady=(8, 0))
            ttk.Spinbox(header, from_=1, to=8, width=4, textvariable=self.threads, state='readonly').pack(side='right')
            ttk.Label(header, text='线程', style='Muted.TLabel').pack(side='right', padx=(12, 8))
            paths = ttk.Frame(area)
            paths.grid(row=1, column=0, sticky='ew', pady=(0, 18))
            paths.columnconfigure(1, weight=1)
            for row, label, variable, command in ((0, '输出目录', self.destination, self.pick_destination), (1, '模板（可选）', self.template, self.pick_template)):
                ttk.Label(paths, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=5)
                entry = ttk.Entry(paths, textvariable=variable)
                entry.grid(row=row, column=1, sticky='ew', pady=5)
                self.mutable.append(entry)
                self.button(paths, '选择', command).grid(row=row, column=2, padx=(10, 0), pady=5)
            tools = ttk.Frame(area)
            tools.grid(row=2, column=0, sticky='ew', pady=(0, 10))
            self.button(tools, '添加 NEV', self.add_files).pack(side='left', padx=(0, 8))
            self.button(tools, '添加文件夹', self.add_folder).pack(side='left', padx=(0, 8))
            self.button(tools, '移除', self.remove).pack(side='left', padx=(0, 8))
            self.button(tools, '清空', self.clear).pack(side='left')
            self.button(tools, '指定模板', self.assign_template).pack(side='right')
            self.button(tools, '无模板', self.no_template).pack(side='right', padx=8)
            table = ttk.Frame(area)
            table.grid(row=3, column=0, sticky='nsew')
            table.rowconfigure(0, weight=1)
            table.columnconfigure(0, weight=1)
            self.tree = ttk.Treeview(table, columns=('source', 'count', 'template', 'progress', 'status'), show='headings', selectmode='extended')
            for name, title, width, stretch, anchor in (
                ('source', '视频', 280, True, 'w'), ('count', '总帧数', 85, False, 'center'),
                ('template', 'NEF 模板', 230, True, 'w'), ('progress', '进度', 120, False, 'center'), ('status', '状态', 100, False, 'center')):
                self.tree.heading(name, text=title, anchor=anchor)
                self.tree.column(name, width=width, minwidth=65, stretch=stretch, anchor=anchor)
            self.tree.grid(row=0, column=0, sticky='nsew')
            scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
            scroll.grid(row=0, column=1, sticky='ns')
            self.tree.configure(yscrollcommand=scroll.set)
            self.tree.tag_configure('failed', foreground='#b42332')
            self.tree.tag_configure('complete', foreground='#168052')
            self.tree.bind('<<TreeviewSelect>>', self.selected)
            self.tree.bind('<Double-1>', self.item_action)
            context = tk.Menu(self.root, tearoff=False)
            context.add_command(label='指定模板', command=self.assign_template)
            context.add_command(label='使用默认模板', command=self.reset_template)
            context.add_command(label='无模板', command=self.no_template)
            context.add_separator()
            context.add_command(label='打开输出文件夹', command=self.open_selected)
            context.add_command(label='移除', command=self.remove)
            def popup(event):
                key = self.tree.identify_row(event.y)
                if key:
                    if key not in self.tree.selection():
                        self.tree.selection_set(key)
                    context.tk_popup(event.x_root, event.y_root)
            self.tree.bind('<Button-3>', popup)
            ttk.Label(area, textvariable=self.detail, style='Muted.TLabel', anchor='w').grid(row=4, column=0, sticky='ew', pady=(8, 13))
            options = ttk.Frame(area)
            options.grid(row=5, column=0, sticky='ew', pady=(0, 16))
            for label, variable, width in (('起始帧', self.first, 7), ('结束帧', self.last, 7), ('间隔', self.step, 5)):
                ttk.Label(options, text=label).pack(side='left', padx=(0, 8))
                entry = ttk.Entry(options, textvariable=variable, width=width)
                entry.pack(side='left', padx=(0, 22))
                self.mutable.append(entry)
            ttk.Label(options, text='帧号从 1 开始 · 结束留空为末帧', style='Muted.TLabel').pack(side='left')
            self.progress = ttk.Progressbar(area, mode='determinate', maximum=1)
            self.progress.grid(row=6, column=0, sticky='ew', pady=(0, 15))
            bottom = ttk.Frame(area)
            bottom.grid(row=7, column=0, sticky='ew')
            ttk.Label(bottom, textvariable=self.summary).pack(side='left')
            self.start_button = ttk.Button(bottom, text='开始', style='Primary.TButton', command=self.start, state='disabled')
            self.start_button.pack(side='right')
            self.stop_button = ttk.Button(bottom, text='停止', command=self.stop, state='disabled')
            self.stop_button.pack(side='right', padx=10)
            ttk.Button(bottom, text='打开输出', command=self.open_destination).pack(side='right', padx=(0, 2))

        def save(self):
            self.data.mkdir(parents=True, exist_ok=True)
            atomic_json(self.config_path, dict(optional_template=True, destination=self.destination.get().strip(), template=self.template.get().strip(),
                                              threads=int(self.threads.get()),
                                              first=self.first.get(), last=self.last.get(), step=self.step.get(),
                                              queue=[dict(source=str(item.source), template=item.template) for item in self.items.values()]))

        def add_files(self):
            if not self.busy:
                self.add_paths(filedialog.askopenfilenames(parent=self.root, title='选择 NEV', filetypes=[('NRAW 视频', '*.nev *.NEV')]))

        def add_folder(self):
            if self.busy:
                return
            directory = filedialog.askdirectory(parent=self.root, title='选择视频文件夹')
            if directory:
                self.scans += 1
                self.refresh_summary()
                def scan():
                    try:
                        paths = []
                        for path in Path(directory).rglob('*'):
                            if self.destroyed.is_set():
                                return
                            if path.suffix.lower() == '.nev' and path.is_file():
                                paths.append(path)
                                if len(paths) >= 10000:
                                    break
                        self.messages.put(('paths', sorted(paths, key=lambda p: str(p).lower())))
                    except OSError as exc:
                        self.messages.put(('scan_error', str(exc)))
                self.probes.submit(scan)

        def add_paths(self, paths):
            if self.busy:
                return
            known = {canonical(item.source) for item in self.items.values()}
            for path in paths:
                if len(self.items) >= 10000:
                    break
                path = Path(path).resolve()
                if path.suffix.lower() != '.nev' or canonical(path) in known:
                    continue
                known.add(canonical(path))
                item = Item(path)
                self.items[item.key] = item
                self.tree.insert('', 'end', iid=item.key)
                self.update_row(item)
                self.probes.submit(self.probe, item.key, path)
            self.refresh_summary()

        def probe(self, key, path):
            try:
                self.messages.put(('probe', key, frame_count(path), ''))
            except Exception as exc:
                self.messages.put(('probe', key, 0, str(exc) or '无法读取视频'))

        def remove(self):
            if not self.busy:
                for key in self.tree.selection():
                    self.items.pop(key, None)
                    self.tree.delete(key)
                self.detail.set('')
                self.refresh_summary()

        def clear(self):
            if not self.busy:
                self.tree.delete(*self.tree.get_children())
                self.items.clear()
                self.detail.set('')
                self.refresh_summary()

        def pick_destination(self):
            value = filedialog.askdirectory(parent=self.root, title='输出目录', initialdir=self.destination.get() if Path(self.destination.get()).is_dir() else str(APP_DIR))
            if value:
                self.destination.set(value)

        def choose_nef(self):
            return filedialog.askopenfilename(parent=self.root, title='选择 NEF 模板', filetypes=[('Nikon RAW', '*.nef *.NEF')])

        def pick_template(self):
            value = self.choose_nef()
            if value:
                self.template.set(value)

        def assign_template(self):
            if self.busy or not self.tree.selection():
                return
            value = self.choose_nef()
            if value:
                for key in self.tree.selection():
                    self.items[key].template = value
                    self.update_row(self.items[key])

        def item_template(self, item):
            return '' if item.template is None else item.template or self.template.get().strip()

        def no_template(self):
            if self.busy:
                return
            selected = self.tree.selection()
            if selected:
                for key in selected:
                    if key in self.items:
                        self.items[key].template = None
                        self.update_row(self.items[key])
            else:
                self.template.set('')
                for item in self.items.values():
                    item.template = None
                    self.update_row(item)
            self.save()

        def reset_template(self):
            if not self.busy:
                for key in self.tree.selection():
                    self.items[key].template = ''
                    self.update_row(self.items[key])

        def refresh_defaults(self):
            for item in self.items.values():
                self.update_row(item)

        def update_row(self, item):
            template = self.item_template(item)
            name = Path(template).name if template else '无模板'
            if template and not item.template:
                name = '默认 · ' + name
            self.tree.item(item.key, values=(item.source.name, item.count or '—', name,
                                            f'{item.done} / {item.total}' if item.total else '—', item.status),
                           tags=('failed',) if item.error else ('complete',) if item.status == '完成' else ())

        def selected(self, _event=None):
            selected = self.tree.selection()
            if selected:
                item = self.items[selected[0]]
                self.detail.set((item.error or str(item.source)).replace('\n', ' ')[:180])

        def item_action(self, event):
            key = self.tree.identify_row(event.y)
            if key:
                item = self.items[key]
                if item.error:
                    messagebox.showerror(item.source.name, item.error, parent=self.root)
                elif item.output and Path(item.output).is_dir():
                    os.startfile(item.output)

        def open_selected(self):
            for key in self.tree.selection()[:1]:
                item = self.items[key]
                if item.output and Path(item.output).is_dir():
                    os.startfile(item.output)

        def open_destination(self):
            destination = Path(self.destination.get().strip())
            if destination.is_dir():
                os.startfile(str(destination))

        def refresh_summary(self):
            if not self.busy:
                count = len(self.items)
                self.summary.set('扫描文件夹' if self.scans else f'{count} 个视频 · {sum(i.count for i in self.items.values()):,} 帧')
                self.start_button.configure(state='normal' if count and not self.scans and all(i.count for i in self.items.values()) else 'disabled')

        def set_busy(self, busy):
            self.busy = busy
            for widget in self.mutable:
                widget.configure(state='disabled' if busy else 'normal')
            self.start_button.configure(state='disabled' if busy else 'normal')
            self.stop_button.configure(state='normal' if busy else 'disabled', text='停止')

        def start(self):
            if self.busy or not self.items:
                return
            try:
                first = int(self.first.get())
                last = int(self.last.get()) if self.last.get().strip() else None
                step = int(self.step.get())
                selection(1, first, last, step)
                for item in self.items.values():
                    value = self.item_template(item)
                    if value and (Path(value).suffix.lower() != '.nef' or not Path(value).is_file()):
                        raise ValueError(f'{item.source.name}：模板不存在，可清空使用无模板模式')
                if not self.destination.get().strip():
                    raise ValueError('请选择输出目录')
                sdk, exiftool = runtime_files()
                threads = int(self.threads.get())
                if not 1 <= threads <= 8:
                    raise ValueError('线程范围：1–8')
                self.save()
                jobs = []
                folders = output_folders([i.source for i in self.items.values()], self.destination.get())
                for item, folder in zip(self.items.values(), folders):
                    item.done = 0
                    item.total = len(selection(item.count, first, last, step))
                    item.output = str(folder)
                    item.error = ''
                    item.status = '等待' if item.total else '无选中帧'
                    self.update_row(item)
                    if item.total:
                        value = self.item_template(item)
                        jobs.append((item.key, dict(source=str(item.source), template=str(Path(value).resolve()) if value else None,
                                                   output=str(folder), sdk=str(sdk.resolve()), exiftool=str(exiftool.resolve()),
                                                   first=first, last=last, step=step, threads=threads)))
                if not jobs:
                    raise ValueError('所选范围内没有视频帧')
                self.progress.configure(maximum=sum(i.total for i in self.items.values()), value=0)
                self.stop_event.clear()
                self.started_at = time.monotonic()
                self.error_count = 0
                self.set_busy(True)
                self.summary.set('准备中')
                threading.Thread(target=self.run_queue, args=(jobs,), daemon=True).start()
            except (OSError, ValueError) as exc:
                self.detail.set(str(exc).replace('\n', ' ')[:180])

        def run_queue(self, jobs):
            try:
                if digest(jobs[0][1]['sdk']) != SDK_SHA256:
                    raise ValueError('解码器版本不匹配')
                run_root = self.data / 'jobs'
                run_root.mkdir(parents=True, exist_ok=True)
                temp = self.data / 'temp'
                temp.mkdir(exist_ok=True)
                environment = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE='1',
                                   OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
                for key, job in jobs:
                    if self.stop_event.is_set():
                        break
                    directory = run_root / uuid.uuid4().hex
                    directory.mkdir()
                    self.active_job = directory
                    path = directory / 'job.json'
                    atomic_json(path, job)
                    self.messages.put(('active', key))
                    command = [sys.executable]
                    if not getattr(sys, 'frozen', False):
                        command.append(str(Path(__file__).resolve()))
                    command += ['--worker', str(path)]
                    log_path = directory / 'worker.log'
                    last_status = {}
                    try:
                        with log_path.open('wb') as log:
                            self.child = subprocess.Popen(command, cwd=str(APP_DIR), env=environment, stdin=subprocess.DEVNULL,
                                                          stdout=log, stderr=log, creationflags=0x08000000)
                            while True:
                                if self.stop_event.is_set():
                                    (directory / 'cancel').touch(exist_ok=True)
                                try:
                                    status = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
                                    if status.get('stamp') != last_status.get('stamp'):
                                        last_status = status
                                        self.messages.put(('status', key, status))
                                except (OSError, ValueError):
                                    pass
                                code = self.child.poll()
                                if code is not None:
                                    break
                                self.stop_event.wait(0.2) if not self.stop_event.is_set() else time.sleep(0.2)
                        try:
                            final = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
                        except (OSError, ValueError):
                            final = last_status
                        if final.get('state') not in ('complete', 'cancelled', 'failed'):
                            log = log_path.read_text(encoding='utf-8', errors='replace')[-4000:]
                            final = dict(state='failed', error=log.strip() or f'转换进程退出（0x{code & 0xffffffff:08X}）', done=last_status.get('done', 0))
                        self.messages.put(('status', key, final))
                    finally:
                        self.child = None
                        self.active_job = None
                        for name in ('job.json', 'status.json', 'cancel', 'worker.log'):
                            (directory / name).unlink(missing_ok=True)
                        directory.rmdir()
            except Exception as exc:
                self.messages.put(('error', str(exc)))
            finally:
                self.messages.put(('finished',))

        def stop(self):
            if self.busy:
                self.stop_event.set()
                self.stop_button.configure(state='disabled', text='停止中')
                self.summary.set('停止中')

        def pump(self):
            try:
                while True:
                    event = self.messages.get_nowait()
                    kind = event[0]
                    if kind == 'paths':
                        self.scans = max(0, self.scans - 1)
                        self.add_paths(event[1])
                    elif kind == 'scan_error':
                        self.scans = max(0, self.scans - 1)
                        self.detail.set(event[1].replace('\n', ' ')[:180])
                        self.refresh_summary()
                    elif kind == 'probe' and event[1] in self.items:
                        item = self.items[event[1]]
                        item.count, item.error = event[2:]
                        item.status = '读取失败' if item.error else '就绪'
                        self.update_row(item)
                        self.refresh_summary()
                    elif kind == 'active':
                        self.current = event[1]
                        self.tree.see(event[1])
                    elif kind == 'status':
                        item = self.items[event[1]]
                        state = event[2]
                        previous_error = bool(item.error)
                        item.done = state.get('done', item.done)
                        item.error = state.get('error', '')
                        item.status = {'loading': '准备中', 'decoding': '解码中', 'writing': '写入中', 'progress': '转换中',
                                       'complete': '完成', 'cancelled': '已停止', 'failed': '失败'}.get(state['state'], state['state'])
                        if item.error and not previous_error:
                            self.error_count += 1
                        self.update_row(item)
                        completed = sum(i.done for i in self.items.values())
                        self.progress.configure(value=completed)
                        self.summary.set(f'{completed:,} / {sum(i.total for i in self.items.values()):,} 帧 · {int(time.monotonic() - self.started_at)} 秒')
                        if self.current == item.key:
                            self.detail.set(item.error.replace('\n', ' ')[:180] if item.error else f'{item.source.name}  ·  {item.status}')
                    elif kind == 'error':
                        self.detail.set(event[1].replace('\n', ' ')[:180])
                        self.error_count += 1
                    elif kind == 'finished':
                        for item in self.items.values():
                            if item.status == '等待':
                                item.status = '已停止' if self.stop_event.is_set() else '未处理'
                                self.update_row(item)
                        self.set_busy(False)
                        completed = sum(i.done for i in self.items.values())
                        status = '已停止' if self.stop_event.is_set() else '完成'
                        self.summary.set(f'{status} · {completed:,} 帧' + (f' · {self.error_count} 项失败' if self.error_count else ''))
                        if self.closing:
                            self.destroy()
                            return
            except queue.Empty:
                pass
            self.root.after(100, self.pump)

        def close(self):
            if self.busy:
                if messagebox.askyesno('退出', '停止转换并退出？', parent=self.root):
                    self.closing = True
                    self.stop()
                return
            self.destroy()

        def destroy(self):
            self.destroyed.set()
            try:
                self.save()
            except (OSError, ValueError):
                pass
            self.probes.shutdown(wait=False, cancel_futures=True)
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
