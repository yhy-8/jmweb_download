import os
import shutil
import uuid
import time
import asyncio
import re
import urllib.parse
from nicegui import ui, app
from fastapi.responses import StreamingResponse
import jmcomic

BASE_TEMP_DIR = os.path.abspath('./temp_downloads')
os.makedirs(BASE_TEMP_DIR, exist_ok=True)

# ===== 全局并发控制变量 =====
MAX_CONCURRENT_DOWNLOADS = 3
current_downloads = 0

# ===== 用于通信的事件字典 =====
# 记录每个下载任务的完成状态: { session_id: asyncio.Event() }
download_events = {}


def clean_old_zips():
    now = time.time()
    for filename in os.listdir(BASE_TEMP_DIR):
        if filename.endswith('.zip') or filename.endswith('.cbz'):
            file_path = os.path.join(BASE_TEMP_DIR, filename)
            if now - os.path.getmtime(file_path) > 3600:
                try:
                    os.remove(file_path)
                except Exception:
                    pass


def check_download_and_zip(album_id: int, workspace: str) -> tuple[str, str]:
    option = jmcomic.create_option_by_file('./option.yml')
    option.dir_rule.base_dir = workspace
    client = option.build_jm_client()
    album = client.get_album_detail(album_id)

    # 获取真实的漫画名称，并清理掉不能用作文件名的特殊字符
    # 兼容处理：不同版本的 jmcomic 属性可能叫 title 或 name
    raw_name = getattr(album, 'title', getattr(album, 'name', f"漫画_{album_id}"))
    safe_manga_name = re.sub(r'[\\/*?:"<>|]', "", raw_name).strip()

    if len(album) > 1:
        raise ValueError(f"拦截：该漫画共有 {len(album)} 话。\n为防止 3M 小带宽被撑爆，系统仅允许下载单话漫画！")

    jmcomic.download_album(album_id, option)
    # 服务器本地依然保留带 UUID 的文件名防冲突
    zip_filename = f"manga_{album_id}_{uuid.uuid4().hex[:6]}"
    zip_filepath = os.path.join(BASE_TEMP_DIR, zip_filename)

    created_zip = shutil.make_archive(zip_filepath, 'zip', workspace)
    shutil.rmtree(workspace, ignore_errors=True)

    cbz_filepath = created_zip[:-4] + '.cbz'
    os.rename(created_zip, cbz_filepath)

    # 返回文件路径和安全的漫画名称
    return cbz_filepath, safe_manga_name


# ===== 接管下载的底层路由 =====
@app.get('/download_manga/{session_id}/{filename}')
async def download_manga(session_id: str, filename: str, download_name: str = "manga.cbz"):
    """自定义流式下载路由，用于感知下载是否结束并自定义下载文件名"""
    filepath = os.path.join(BASE_TEMP_DIR, filename)

    async def file_iterator():
        try:
            # 以 64KB 为一个块读取文件并发送给浏览器
            with open(filepath, "rb") as f:
                while chunk := f.read(64 * 1024):
                    yield chunk
        finally:
            # 核心机制：无论下载是顺利完成，还是用户取消/断网，都会走到这里
            if session_id in download_events:
                download_events[session_id].set()  # 通知 UI 界面：下载过程结束！

    # 【核心修复】：重新对 FastAPI 解码后的中文进行 URL 编码
    encoded_header_name = urllib.parse.quote(download_name)

    # 使用 filename*=utf-8'' 格式，并将编码后的名称传给浏览器
    headers = {"Content-Disposition": f"attachment; filename*=utf-8''{encoded_header_name}"}
    return StreamingResponse(file_iterator(), media_type="application/zip", headers=headers)


@ui.page('/')
def index():
    with ui.column().classes('w-full min-h-screen items-center justify-center p-4 bg-gray-50'):
        ui.markdown('### JM简易下载器').classes('text-2xl font-bold mb-2 text-center')

        status_label = ui.label(f'🔥 当前服务器任务数: {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}').classes(
            'text-sm font-bold text-red-500 mb-2 text-center'
        )
        ui.timer(1.0, lambda: status_label.set_text(
            f'🔥 当前服务器任务数: {current_downloads}/{MAX_CONCURRENT_DOWNLOADS}'))

        ui.label('💡 提示：下载的文件将使用 .cbz 漫画专用格式，可直接用各类漫画APP打开。').classes(
            'text-blue-500 text-sm mb-6 text-center max-w-md'
        )

        with ui.card().classes('w-full max-w-md shadow-md'):
            id_input = ui.input('请输入漫画ID (仅限单话的漫画)').classes('w-full')

            async def on_download_click():
                global current_downloads

                album_id_str = id_input.value
                if not album_id_str or not album_id_str.isdigit():
                    ui.notify('请输入正确的纯数字ID', type='warning')
                    return

                if current_downloads >= MAX_CONCURRENT_DOWNLOADS:
                    ui.notify('服务器当前下载人数已满 (3/3)，小机器快冒烟啦，请稍后再试！', type='warning', timeout=5000)
                    return

                # 1. 立即占用名额，禁用按钮
                current_downloads += 1
                btn.disable()
                spinner = ui.spinner(size='lg')

                album_id = int(album_id_str)
                workspace = os.path.join(BASE_TEMP_DIR, uuid.uuid4().hex)
                os.makedirs(workspace, exist_ok=True)
                session_id = str(uuid.uuid4())  # 生成唯一的本次下载任务 ID

                try:
                    # 2. 清理旧文件并开始打包
                    clean_old_zips()
                    ui.notify(f'正在龟速打包 {album_id}，需要一到两分钟，请勿刷新或关闭网站...', type='info')

                    # 获取纯文件名和真实的漫画名称
                    cbz_path, safe_manga_name = await asyncio.to_thread(check_download_and_zip, album_id, workspace)
                    filename = os.path.basename(cbz_path)

                    # 3. 准备等待浏览器下载完成
                    download_events[session_id] = asyncio.Event()
                    ui.notify(f'打包完成！《{safe_manga_name}》即将开始下载。下载期间请勿关闭本页面！', type='positive',
                              timeout=8000)

                    # 核心改动：将中文文件名进行 URL 编码，拼接到请求地址中
                    encoded_name = urllib.parse.quote(f"{safe_manga_name}.cbz")
                    # 触发浏览器通过自定义路由下载
                    ui.download(f'/download_manga/{session_id}/{filename}?download_name={encoded_name}')

                    # 4. 阻塞 UI，直到流式传输路由发送 `set()` 信号（或超时）
                    # 设置 1800 秒 (30 分钟) 超时，防止用户的浏览器拦截弹窗导致事件永远卡死
                    await asyncio.wait_for(download_events[session_id].wait(), timeout=1800.0)

                    ui.notify(f'✅ 《{safe_manga_name}》本地下载完成，感谢使用！', type='positive')

                except asyncio.TimeoutError:
                    ui.notify('下载超时或被浏览器拦截。', type='warning')
                except ValueError as ve:
                    ui.notify(str(ve), type='negative', multi_line=True, timeout=5000)
                except Exception as e:
                    ui.notify(f'发生错误: {str(e)}', type='negative')
                finally:
                    # 5. 无论是彻底下载完，还是超时，还是打包出错，最终都在这里释放名额
                    shutil.rmtree(workspace, ignore_errors=True)
                    if session_id in download_events:
                        del download_events[session_id]  # 清理内存

                    current_downloads -= 1
                    btn.enable()
                    spinner.delete()

            btn = ui.button('检测并下载', on_click=on_download_click).classes('w-full mt-4')

        ui.label(
            '注意：服务器上传带宽仅 3M (约 375KB/s)，打包完成后浏览器下载可能需要几分钟，非必要一般不使用该网站下载。').classes(
            'text-gray-500 text-sm mb-2 text-center max-w-md'
        )

        ui.label('另外：大大大大大哥别打我，该服务器没有做任何渗透测试和防护措施ToT').classes(
            'text-blue-500 text-sm mb-6 text-center max-w-md'
        )


# 如果要在公网访问，建议将 host 设为 '0.0.0.0'
ui.run(title="WSG的私有下载节点", host='0.0.0.0', port=80, show=False)