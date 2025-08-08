import zipfile
import os
import shutil
import json
import requests
import tempfile
import time
import threading
import traceback
import re
from itertools import islice

# --- GUI Libraries ---
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox

# --- 后端逻辑 (翻译函数) ---

def log_message(text_widget, message):
    """向 GUI 的文本小部件中插入一条消息。"""
    if text_widget:
        text_widget.insert(tk.END, message + "\n")
        text_widget.see(tk.END)

def extract_archive(archive_path, extract_dir):
    """通用解压函数，适用于 .mcpack 和 .mcaddon"""
    with zipfile.ZipFile(archive_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)

# --- 更新：硬编码字符串扫描与提取 ---

def find_pack_root(start_path):
    """从给定路径向上查找，直到找到包含 manifest.json 的目录"""
    current_path = os.path.dirname(start_path)
    while current_path and current_path != os.path.dirname(current_path):
        if 'manifest.json' in os.listdir(current_path):
            return current_path
        current_path = os.path.dirname(current_path)
    return None

def traverse_and_collect(obj, strings_to_translate):
    """递归遍历数据结构，收集需要翻译的字符串。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            # 翻译物品/实体名称
            if key == "minecraft:display_name" and isinstance(value, dict) and "value" in value:
                display_name_value = value.get("value")
                # 确保是字符串且不是语言键
                if isinstance(display_name_value, str) and display_name_value.strip() and not (display_name_value.startswith("item.") or display_name_value.startswith("tile.")):
                    if display_name_value not in strings_to_translate:
                        strings_to_translate.append(display_name_value)
            # 翻译物品描述 (lore)
            elif key == "minecraft:item_lore" and isinstance(value, dict) and "value" in value:
                lore_list = value.get("value")
                if isinstance(lore_list, list):
                    for lore_line in lore_list:
                        if isinstance(lore_line, str) and lore_line.strip():
                             if lore_line not in strings_to_translate:
                                strings_to_translate.append(lore_line)
            else:
                traverse_and_collect(value, strings_to_translate)
    elif isinstance(obj, list):
        for item in obj:
            traverse_and_collect(item, strings_to_translate)

def traverse_and_replace(obj, translated_map):
    """递归遍历数据结构，使用翻译好的字符串进行替换。"""
    modified = False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "minecraft:display_name" and isinstance(value, dict) and "value" in value:
                original_text = value.get("value")
                if original_text in translated_map:
                    value["value"] = translated_map[original_text]
                    modified = True
            elif key == "minecraft:item_lore" and isinstance(value, dict) and "value" in value:
                 lore_list = value.get("value")
                 if isinstance(lore_list, list):
                    new_lore = [translated_map.get(line, line) for line in lore_list]
                    if new_lore != lore_list:
                        value["value"] = new_lore
                        modified = True
            else:
                if traverse_and_replace(value, translated_map):
                    modified = True
    elif isinstance(obj, list):
        for item in obj:
            if traverse_and_replace(item, translated_map):
                modified = True
    return modified

def process_hardcoded_strings(temp_dir, text_widget, api_url, api_key, model_name, pause_event):
    """主函数：扫描、翻译并直接替换JSON文件中的硬编码字符串（使用更安全的文件I/O）"""
    log_message(text_widget, "--- 开始直接翻译硬编码字符串 (安全模式) ---")
    all_json_files = [os.path.join(root, file) for root, _, files in os.walk(temp_dir) for file in files if file.endswith('.json')]
    
    strings_to_translate = []
    file_data_map = {}

    # 1. 读取所有JSON文件到内存，并收集待翻译字符串
    for json_path in all_json_files:
        if os.path.basename(os.path.dirname(json_path)) == 'texts':
            continue
        try:
            with open(json_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
                file_data_map[json_path] = data
                traverse_and_collect(data, strings_to_translate)
        except (IOError, json.JSONDecodeError):
            log_message(text_widget, f"警告：跳过无法读取或解析的文件 {os.path.basename(json_path)}")
            continue

    if not strings_to_translate:
        log_message(text_widget, "未找到需要直接翻译的硬编码字符串。")
        return

    log_message(text_widget, f"找到 {len(strings_to_translate)} 个独特的硬编码字符串，准备批量翻译...")

    # 2. 批量翻译
    to_translate_dict = {f"key_{i}": s for i, s in enumerate(strings_to_translate)}
    pause_event.wait()
    translated_dict = translate_batch(to_translate_dict, text_widget, api_url, api_key, model_name)

    if not translated_dict:
        log_message(text_widget, "❌ 硬编码字符串批量翻译失败，跳过直接替换步骤。")
        return
        
    translated_map = {original: translated for original, translated in zip(strings_to_translate, translated_dict.values())}
    log_message(text_widget, "翻译完成，正在将译文写回 JSON 文件...")

    # 3. 在内存中进行替换，然后一次性写回文件
    replaced_count = 0
    for json_path, data in file_data_map.items():
        if traverse_and_replace(data, translated_map):
            try:
                # 使用 'w' 模式完全重写文件，这是最安全的方式
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                replaced_count += 1
            except IOError:
                log_message(text_widget, f"❌ 写入文件失败: {os.path.basename(json_path)}")

    log_message(text_widget, f"✅ 在 {replaced_count} 个文件中完成了硬编码字符串的直接替换。")
    log_message(text_widget, "--- 硬编码字符串直接翻译完成 ---")


# --- 翻译逻辑 (基本不变) ---

def translate_text(text, text_widget, api_url, api_key, model_name):
    """使用通用 API 翻译文本"""
    if not text.strip():
        return text
    if not api_url or not api_key or not model_name:
        log_message(text_widget, "警告：API 地址、密钥或模型为空，跳过翻译。")
        return text

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "你是一个Minecraft翻译工作者，负责将基岩版addon文件翻译成中文，翻译的结果需要符合Minecraft设定及addon的合理性，只需要给出译文不需要说明。"},
            {"role": "user", "content": text}
        ],
        "temperature": 0.1,
        "stream": False
    }

    retries = 3
    timeout_seconds = 60

    for attempt in range(retries):
        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            result = response.json()
            raw_translated_text = result['choices'][0]['message']['content'].strip()
            # 提取第一行作为纯净的翻译结果，忽略后续所有说明性文字
            translated_text = raw_translated_text.splitlines()[0].strip()
            time.sleep(0.2) # 遵循 API 使用频率限制
            return translated_text
        except requests.exceptions.RequestException as e:
            log_message(text_widget, f"API 请求错误 (尝试 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # 等待 2, 4 秒后重试
                log_message(text_widget, "正在重试...")
            else:
                log_message(text_widget, "已达到最大重试次数，跳过此条目。")
                return text # 所有重试失败后返回原文
        except (KeyError, IndexError) as e:
            log_message(text_widget, f"解析 API 响应失败: {e}")
            return text # 解析错误不重试，直接返回原文
    
    return text # 确保在循环结束后返回原文

def translate_batch(items_dict, text_widget, api_url, api_key, model_name):
    """使用 API 批量翻译文本字典。"""
    if not items_dict:
        return {}

    log_message(text_widget, f"准备批量翻译 {len(items_dict)} 个条目...")
    
    # 将待翻译的字典转换为 JSON 字符串
    input_json_str = json.dumps(items_dict, ensure_ascii=False, indent=2)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "你是一个Minecraft翻译工作者。请将用户提供的JSON对象中的所有值（value）翻译成简体中文。保持原始的键（key）和JSON结构不变，只返回翻译后的JSON对象，不要添加任何额外的解释或说明。"},
            {"role": "user", "content": input_json_str}
        ],
        "temperature": 0.1,
        "stream": False
    }

    retries = 3
    timeout_seconds = 180 # 批量翻译需要更长的超时时间

    for attempt in range(retries):
        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            response_text = response.json()['choices'][0]['message']['content'].strip()
            
            # 清理 AI 可能返回的代码块标记
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]

            translated_dict = json.loads(response_text)
            log_message(text_widget, "批量翻译成功！")
            return translated_dict
        except requests.exceptions.RequestException as e:
            log_message(text_widget, f"API 批量请求错误 (尝试 {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                log_message(text_widget, "正在重试...")
            else:
                log_message(text_widget, "已达到最大重试次数，批量翻译失败。")
                return None
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            log_message(text_widget, f"解析批量翻译响应失败: {e}。将尝试逐条翻译。")
            return None # 返回 None 表示批量失败，应回退到单条模式
    return None

def chunk_dict(data, size=20):
    """将字典分块"""
    it = iter(data)
    for _ in range(0, len(data), size):
        yield {k: data[k] for k in islice(it, size)}

def translate_json_file(filepath, text_widget, api_url, api_key, model_name, progress_state, pause_event):
    log_message(text_widget, f"正在处理 .json 文件: {os.path.basename(filepath)}")
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as file:
            data = json.load(file)
    except (UnicodeDecodeError, json.JSONDecodeError, IOError) as e:
        log_message(text_widget, f"警告: 读取或解析 {os.path.basename(filepath)} 失败，已跳过。错误: {e}")
        return

    to_translate = {key: value for key, value in data.items() if isinstance(value, str) and value.strip()}
    
    if not to_translate:
        log_message(text_widget, "文件中无内容需要翻译。")
        return

    final_translated_data = {}
    batch_failed = False

    for chunk in chunk_dict(to_translate):
        pause_event.wait()
        translated_chunk = translate_batch(chunk, text_widget, api_url, api_key, model_name)
        
        if translated_chunk is None:
            batch_failed = True
            break # 如果任何一个批次失败，则中断并回退
        
        final_translated_data.update(translated_chunk)
        progress_state['current'] += len(chunk)
        log_message(text_widget, f"批次处理完成，总进度 ({progress_state['current']}/{progress_state['total']})")

    if batch_failed: # 批量失败，回退到单条翻译
        log_message(text_widget, "回退到逐条翻译模式...")
        # 重置进度，因为之前是按批次加的
        progress_state['current'] -= len(final_translated_data)
        final_translated_data = {}
        for key, value in to_translate.items():
            pause_event.wait()
            progress_state['current'] += 1
            translated_value = translate_text(value, text_widget, api_url, api_key, model_name)
            final_translated_data[key] = translated_value
            log_message(text_widget, f"进度 ({progress_state['current']}/{progress_state['total']}): {value} > {translated_value}")

    # 合并翻译好的和不需要翻译的
    final_data = data.copy()
    final_data.update(final_translated_data)
    
    # 备份原始文件并用译文覆盖
    backup_path = filepath + ".bak"
    try:
        if not os.path.exists(backup_path):
            shutil.copy2(filepath, backup_path)
            log_message(text_widget, f"已备份原始文件到: {os.path.basename(backup_path)}")
    except Exception as e:
        log_message(text_widget, f"备份文件失败: {e}")
        return

    with open(filepath, 'w', encoding='utf-8') as file:
        json.dump(final_data, file, ensure_ascii=False, indent=2)

def translate_lang_file(filepath, text_widget, api_url, api_key, model_name, progress_state, pause_event):
    log_message(text_widget, f"正在处理 .lang 文件: {os.path.basename(filepath)}")
    try:
        with open(filepath, 'r', encoding='utf-8') as file:
            lines = file.readlines()
    except (UnicodeDecodeError, IOError):
        with open(filepath, 'r', encoding='utf-8-sig') as file:
            lines = file.readlines()

    original_lines = {}
    to_translate = {}
    line_keys = [] # 保持原始顺序
    
    for i, line in enumerate(lines):
        line_stripped = line.rstrip('\r\n')
        if "=" in line_stripped and not line_stripped.strip().startswith("#"):
            parts = line_stripped.split("=", 1)
            key, value = parts[0], parts[1]
            if value.strip():
                unique_key = f"{key}_{i}" # 使用唯一键
                to_translate[unique_key] = value
                original_lines[unique_key] = (key, value)
                line_keys.append(unique_key)
            else:
                line_keys.append(line) # 不需要翻译的行
        else:
            line_keys.append(line) # 注释或空行

    if not to_translate:
        log_message(text_widget, "文件中无内容需要翻译。")
        return

    final_translated_values = {}
    batch_failed = False

    for chunk in chunk_dict(to_translate):
        pause_event.wait()
        translated_chunk = translate_batch(chunk, text_widget, api_url, api_key, model_name)

        if translated_chunk is None:
            batch_failed = True
            break

        final_translated_values.update(translated_chunk)
        progress_state['current'] += len(chunk)
        log_message(text_widget, f"批次处理完成，总进度 ({progress_state['current']}/{progress_state['total']})")

    if batch_failed: # 批量失败，回退
        log_message(text_widget, "回退到逐条翻译模式...")
        progress_state['current'] -= len(final_translated_values)
        final_translated_values = {}
        for unique_key, value in to_translate.items():
            pause_event.wait()
            progress_state['current'] += 1
            translated_value = translate_text(value, text_widget, api_url, api_key, model_name)
            final_translated_values[unique_key] = translated_value
            log_message(text_widget, f"进度 ({progress_state['current']}/{progress_state['total']}): {value} > {translated_value}")

    # 构建新文件内容
    final_lines = []
    for key_or_line in line_keys:
        if isinstance(key_or_line, str) and key_or_line in final_translated_values:
            original_key, _ = original_lines[key_or_line]
            final_lines.append(f"{original_key}={final_translated_values[key_or_line]}\n")
        else:
            final_lines.append(key_or_line)

    # 备份原始文件并用译文覆盖
    backup_path = filepath + ".bak"
    try:
        if not os.path.exists(backup_path):
            shutil.copy2(filepath, backup_path)
            log_message(text_widget, f"已备份原始文件到: {os.path.basename(backup_path)}")
    except Exception as e:
        log_message(text_widget, f"备份文件失败: {e}")
        return

    with open(filepath, 'w', encoding='utf-8') as out:
        out.writelines(final_lines)


def process_translations(texts_dirs, text_widget, api_url, api_key, model_name, pause_event):
    # 1. 统计所有 'texts' 文件夹中需要翻译的总条目数
    total_items = 0
    files_to_process = []
    for texts_dir in texts_dirs:
        for root, _, files in os.walk(texts_dir):
            for file in files:
                if file == "en_US.lang" or file == "en_US.json":
                    filepath = os.path.join(root, file)
                    files_to_process.append(filepath)
                    try:
                        if file.endswith(".lang"):
                            with open(filepath, 'r', encoding='utf-8') as f:
                                for line in f:
                                    if "=" in line and not line.strip().startswith("#") and line.split("=", 1)[1].strip():
                                        total_items += 1
                        elif file.endswith(".json"):
                            with open(filepath, 'r', encoding='utf-8-sig') as f:
                                content = f.read()
                                if content.strip():
                                    data = json.loads(content)
                                    for value in data.values():
                                        if isinstance(value, str) and value.strip():
                                            total_items += 1
                    except Exception as e:
                        log_message(text_widget, f"警告：统计文件 {file} 时出错，已跳过。错误: {e}")

    if total_items == 0:
        log_message(text_widget, "在 'texts' 文件夹中未找到可翻译的英文内容 (en_US.lang/json)。")
        return

    log_message(text_widget, f"已找到 {total_items} 个待翻译条目。")
    
    # 2. 开始翻译并更新进度
    progress_state = {'current': 0, 'total': total_items}
    for filepath in files_to_process:
        if filepath.endswith(".lang"):
            translate_lang_file(filepath, text_widget, api_url, api_key, model_name, progress_state, pause_event)
        elif filepath.endswith(".json"):
            translate_json_file(filepath, text_widget, api_url, api_key, model_name, progress_state, pause_event)


def repackage_archive(processed_dir, output_path):
    shutil.make_archive(output_path.rsplit('.', 1)[0], 'zip', processed_dir)
    os.rename(output_path.rsplit('.', 1)[0] + ".zip", output_path)

# --- 主应用逻辑 ---

def test_api_connection_thread(api_url, api_key, model_name, text_widget, test_button):
    """在新线程中测试 API 连接，以避免 GUI 冻结。"""
    def run():
        test_button.config(state=tk.DISABLED)
        log_message(text_widget, "\n--- 正在测试 API 连接... ---")

        if not api_url or not api_key or not model_name:
            log_message(text_widget, "❌ 错误：API 地址、密钥或模型为空。")
            messagebox.showerror("测试失败", "API 地址、密钥和模型名称不能为空！")
            test_button.config(state=tk.NORMAL)
            return

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.1,
            "stream": False
        }

        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            response.json()
            log_message(text_widget, "✅ API 连接成功！")
            messagebox.showinfo("成功", "API 连接成功！")
        except requests.exceptions.RequestException as e:
            error_message = f"API 请求错误: {e}"
            log_message(text_widget, f"❌ {error_message}")
            messagebox.showerror("测试失败", f"连接失败，请检查 API 地址、密钥和网络连接。\n\n详细信息: {e}")
        except Exception as e:
            error_message = f"发生未知错误: {e}"
            log_message(text_widget, f"❌ {error_message}")
            messagebox.showerror("测试失败", f"发生未知错误。\n\n详细信息: {e}")
        finally:
            test_button.config(state=tk.NORMAL)

    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()


def start_translation_thread(mc_file_path, api_url, api_key, model_name, text_widget, start_button, pause_button, pause_event):
    """在新线程中运行翻译流程，以避免 GUI 冻结。"""
    def run():
        try:
            start_button.config(state=tk.DISABLED)
            pause_button.config(state=tk.NORMAL)
            pause_event.set() # 确保开始时是运行状态
            log_message(text_widget, "--- 开始翻译流程 ---")

            if not mc_file_path or not os.path.exists(mc_file_path):
                log_message(text_widget, "❌ 错误：请输入有效的文件路径！")
                messagebox.showerror("错误", "请输入有效的文件路径！")
                return
            
            if not (mc_file_path.endswith(".mcpack") or mc_file_path.endswith(".mcaddon")):
                log_message(text_widget, "❌ 错误：请选择 .mcpack 或 .mcaddon 文件。")
                messagebox.showerror("错误", "请选择 .mcpack 或 .mcaddon 文件。")
                return

            if not api_url or not api_key or not model_name:
                log_message(text_widget, "❌ 错误：请在 API 设置中填写完整的 API 地址、密钥和模型名称。")
                messagebox.showerror("错误", "请在 API 设置中填写完整的 API 地址、密钥和模型名称。")
                return

            with tempfile.TemporaryDirectory() as tmpdir:
                log_message(text_widget, f"📦 解压中 -> {tmpdir}")
                extract_archive(mc_file_path, tmpdir)

                # --- 关键修改：处理嵌套的 .mcpack 文件 (来自 .mcaddon) ---
                log_message(text_widget, "  -> 正在检查嵌套的 .mcpack 文件...")
                mcpacks_found = [os.path.join(root, file) for root, _, files in os.walk(tmpdir) for file in files if file.endswith(".mcpack")]
                
                if mcpacks_found:
                    log_message(text_widget, f"  -> 发现 {len(mcpacks_found)} 个 .mcpack，将进行二次解压。")
                    for mcpack_path in mcpacks_found:
                        # 解压到与 .mcpack 文件同名的文件夹内
                        pack_extract_dir = os.path.splitext(mcpack_path)[0]
                        os.makedirs(pack_extract_dir, exist_ok=True)
                        try:
                            log_message(text_widget, f"    -> 正在解压: {os.path.basename(mcpack_path)}")
                            extract_archive(mcpack_path, pack_extract_dir)
                            # 成功解压后移除原始 .mcpack 文件
                            os.remove(mcpack_path)
                        except Exception as e:
                            log_message(text_widget, f"    -> ❌ 解压 {os.path.basename(mcpack_path)} 失败: {e}")
                else:
                    log_message(text_widget, "  -> 未发现嵌套的 .mcpack 文件。")
                # --- 嵌套处理结束 ---

                # 直接翻译并替换硬编码字符串
                process_hardcoded_strings(tmpdir, text_widget, api_url, api_key, model_name, pause_event)

                # 在整个解压目录中搜索所有 'texts' 文件夹
                texts_dirs = []
                for root, dirs, _ in os.walk(tmpdir):
                    if 'texts' in dirs:
                        texts_dirs.append(os.path.join(root, 'texts'))
                
                if not texts_dirs:
                    log_message(text_widget, "⚠️ 警告：在文件中未找到 'texts' 文件夹，将跳过语言文件翻译。")
                else:
                    log_message(text_widget, f"✅ 找到 {len(texts_dirs)} 个 'texts' 文件夹，准备处理语言文件。")
                    log_message(text_widget, f"🌐 开始翻译语言文件 (使用 {model_name})...")
                    process_translations(texts_dirs, text_widget, api_url, api_key, model_name, pause_event)

                # 根据原始文件类型决定输出文件名
                if mc_file_path.endswith(".mcpack"):
                    out_path = mc_file_path.replace(".mcpack", "_translated.mcpack")
                else:
                    out_path = mc_file_path.replace(".mcaddon", "_translated.mcaddon")

                log_message(text_widget, "📦 重新打包中...")
                repackage_archive(tmpdir, out_path)

                log_message(text_widget, "--------------------")
                log_message(text_widget, f"✅ 翻译完成！文件保存为：{out_path}")

        except Exception:
            log_message(text_widget, "\n❌ 程序发生未预料的错误：")
            log_message(text_widget, traceback.format_exc())
            messagebox.showerror("严重错误", "发生未预料的错误，请查看日志获取详情。")
        finally:
            start_button.config(state=tk.NORMAL)
            pause_button.config(text="暂停", state=tk.DISABLED)

    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()

# --- GUI 设置 ---

def create_gui():
    root = tk.Tk()
    
    root.title("Minecraft Addon ai简单翻译工具 - by Yuzirael")

    try:
        root.iconbitmap('my_icon.ico')
    except tk.TclError:
        # 如果找不到图标文件，程序会使用默认图标，不会报错
        print("提示：未找到图标文件 my_icon.ico，将使用默认图标。")

    root.geometry("800x600")

    # 文件选择框架
    file_frame = tk.LabelFrame(root, text="MCPACK / MCADDON 文件", padx=10, pady=10)
    file_frame.pack(fill=tk.X, padx=10, pady=10)

    filepath_var = tk.StringVar()
    file_entry = tk.Entry(file_frame, textvariable=filepath_var, state='readonly')
    file_entry.pack(fill=tk.X, expand=True, side=tk.LEFT, padx=(0, 5))

    def select_file():
        filename = filedialog.askopenfilename(
            title="选择 .mcpack 或 .mcaddon 文件",
            filetypes=(("Minecraft Addons", "*.mcpack *.mcaddon"), ("所有文件", "*.*"))
        )
        if filename:
            filepath_var.set(filename)

    browse_button = tk.Button(file_frame, text="选择文件...", command=select_file)
    browse_button.pack(side=tk.LEFT)

    # API 设置框架
    api_frame = tk.LabelFrame(root, text="API 设置", padx=10, pady=10)
    api_frame.pack(fill=tk.X, padx=10, pady=5)

    api_url_var = tk.StringVar(value="https://api.deepseek.com/chat/completions")
    api_key_var = tk.StringVar()
    model_name_var = tk.StringVar(value="deepseek-chat")

    tk.Label(api_frame, text="API 地址:").grid(row=0, column=0, sticky="w", pady=2)
    tk.Entry(api_frame, textvariable=api_url_var).grid(row=0, column=1, sticky="ew", padx=5)

    tk.Label(api_frame, text="API 密钥:").grid(row=1, column=0, sticky="w", pady=2)
    tk.Entry(api_frame, textvariable=api_key_var, show="*").grid(row=1, column=1, sticky="ew", padx=5)
    
    tk.Label(api_frame, text="模型名称:").grid(row=2, column=0, sticky="w", pady=2)
    tk.Entry(api_frame, textvariable=model_name_var).grid(row=2, column=1, sticky="ew", padx=5)

    api_frame.columnconfigure(1, weight=1)

    test_api_button = tk.Button(api_frame, text="测试 API 连接")
    test_api_button.grid(row=3, column=0, columnspan=2, pady=(5, 0), sticky="ew")

    # 日志区域
    log_frame = tk.LabelFrame(root, text="运行日志", padx=10, pady=5)
    log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    log_widget = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state=tk.NORMAL, height=10)
    log_widget.pack(fill=tk.BOTH, expand=True)
    
    log_message(log_widget, "欢迎使用ai简单翻译工具！\n1. 在 API 设置中填入您的 API 地址、密钥和模型。\n2. 点击 '选择文件...' 选择您的文件。\n3. 点击 '开始翻译'。\n4. 翻译速度取决于api的响应速度，请耐心等待。")

    # 为测试按钮绑定命令
    test_api_button.config(command=lambda: test_api_connection_thread(
        api_url_var.get(),
        api_key_var.get(),
        model_name_var.get(),
        log_widget,
        test_api_button
    ))

    # 暂停/继续逻辑
    pause_event = threading.Event()
    def toggle_pause():
        if pause_event.is_set():
            pause_event.clear()
            pause_resume_button.config(text="继续")
            log_message(log_widget, "--- 已暂停 ---")
        else:
            pause_event.set()
            pause_resume_button.config(text="暂停")
            log_message(log_widget, "--- 继续翻译 ---")

    # 按钮框架
    button_frame = tk.Frame(root)
    button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
    button_frame.columnconfigure(0, weight=1)
    button_frame.columnconfigure(1, weight=1)

    # 开始按钮
    start_button = tk.Button(
        button_frame,
        text="开始翻译",
        font=("Helvetica", 12, "bold"),
    )
    start_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))

    # 暂停/继续按钮
    pause_resume_button = tk.Button(
        button_frame,
        text="暂停",
        font=("Helvetica", 12),
        state=tk.DISABLED,
        command=toggle_pause
    )
    pause_resume_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

    start_button.config(command=lambda: start_translation_thread(
        filepath_var.get(),
        api_url_var.get(),
        api_key_var.get(),
        model_name_var.get(),
        log_widget,
        start_button,
        pause_resume_button,
        pause_event
    ))

    root.mainloop()

if __name__ == "__main__":
    create_gui()