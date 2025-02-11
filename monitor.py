from xhs import XhsClient
import time
from typing import List
from config import XHS_CONFIG, WECOM_CONFIG, MONITOR_CONFIG
from utils import xhs_sign
from db import Database
from wecom import WecomMessage
from comment_generator import CommentGenerator
import threading
from concurrent.futures import ThreadPoolExecutor

class XHSMonitor:
    def __init__(self, cookie: str, webhook_url: str):
        # 初始化小红书客户端，传入 Cookie 和签名
        self.client = XhsClient(cookie=cookie, sign=xhs_sign)
        self.wecom = WecomMessage(webhook_url)
        self.db = Database()
        self.error_count = 0
        self.comment_generator = CommentGenerator()
        self.lock = threading.Lock()  # 用于保护数据库等共享资源
        # 创建线程池，最大同时运行5个线程
        self.executor = ThreadPoolExecutor(max_workers=5)
        
    def send_error_notification(self, error_msg: str):
        """
        发送错误通知
        :param error_msg: 错误信息
        """
        time_str = time.strftime('%Y-%m-%d %H:%M:%S')
        content = (
            "小红书监控异常告警\n"
            f"错误信息：{error_msg}\n"
            f"告警时间：{time_str}"
        )
        self.wecom.send_text(content)
    
    def get_latest_notes(self, user_id: str) -> List[dict]:
        """
        获取用户最新笔记
        :param user_id: 用户ID
        :return: 笔记列表
        """
        try:
            # 请求获取用户笔记数据
            res_data = self.client.get_user_notes(user_id)
            self.error_count = 0
            return res_data.get('notes', [])
            
        except Exception as e:
            # 如果发生错误，记录日志并进行重试
            error_msg = str(e)
            print(f"获取用户笔记失败: {error_msg}")
            time.sleep(60)

            self.error_count += 1
            # 如果连续错误次数达到阈值，发送告警并终止程序
            if self.error_count >= MONITOR_CONFIG["ERROR_COUNT"]:
                self.send_error_notification(f"API 请求失败\n详细信息：{error_msg}")
                exit(-1)

            return []

    def like_note(self, note_id: str) -> bool:
        """
        点赞笔记
        :param note_id: 笔记ID
        :return: 是否成功
        """
        try:
            # 添加点赞延迟，防止操作过快
            time.sleep(MONITOR_CONFIG["LIKE_DELAY"])  # 添加延迟，避免操作过快
            self.client.like_note(note_id)
            print(f"点赞成功: {note_id}")
            return True
        except Exception as e:
            print(f"点赞失败: {e}")
            return False

    def get_note_detail(self, note_id: str, xsec: str) -> dict:
        """
        获取笔记详细信息
        :param note_id: 笔记ID
        :return: 笔记详细信息
        """
        try:
             # 请求笔记详情
            uri = '/api/sns/web/v1/feed'
            data = {"source_note_id": note_id, "image_formats": ["jpg", "webp", "avif"], "extra": {"need_body_topic": "1"}, "xsec_source": "pc_search", "xsec_token": xsec}
            res = self.client.post(uri, data=data)
            note_detail = res["items"][0]["note_card"]
            return note_detail
        except Exception as e:
            print(f"获取笔记详情失败: {e}")
            return {}

    def comment_note(self, note_id: str, note_data: dict) -> dict:
        """
        评论笔记
        :param note_id: 笔记ID
        :param note_data: 笔记数据
        :return: 评论结果
        """
        try:
            # 添加评论延迟，防止操作过快
            time.sleep(MONITOR_CONFIG["COMMENT_DELAY"])
            note_detail = self.get_note_detail(note_id, note_data.get('xsec_token', ''))
            title = note_detail.get('title', '')
            content = note_detail.get('desc', '')
            note_type = '视频' if note_detail.get('type') == 'video' else '图文'
            content = f"这是一个{note_type}笔记。{content}"
            comment = self.comment_generator.generate_comment(title, content)
            # 在评论生成成功后进行评论
            if comment:
                self.client.comment_note(note_id, comment)
                print(f"评论成功: {note_id} - {comment}")
                return { "comment_status": True, "comment_content": comment }
            else:
                print(f"评论生成失败，未进行评论: {note_id}")
                return { "comment_status": False, "comment_content": "" }
        except Exception as e:
            print(f"评论失败: {e}")
            return { "comment_status": False, "comment_content": "" }

    def interact_with_note(self, note_data: dict) -> dict:
        """
        与笔记互动（点赞+评论）
        :param note_data: 笔记数据
        :return: 互动结果
        """
        result = {
            "like_status": False,
            "comment_status": False,
            "comment_content": ""
        }
        
        if not MONITOR_CONFIG.get("AUTO_INTERACT"):
            return result

        note_id = note_data.get('note_id')
        if not note_id:
            return result
        # 执行点赞操作
        result["like_status"] = self.like_note(note_id)
        # 执行评论操作
        comment_result = self.comment_note(note_id, note_data)

        result["comment_status"] = comment_result["comment_status"]
        result["comment_content"] = comment_result["comment_content"]
        
        return result

    def send_note_notification(self, note_data: dict, interact_result: dict = None):
        """
        发送笔记通知
        :param note_data: 笔记数据
        :param interact_result: 互动结果
        """
        note_url = f"https://www.xiaohongshu.com/explore/{note_data.get('note_id')}"
        user_name = note_data.get('user', {}).get('nickname', '未知用户')
        title = note_data.get('display_title', '无标题')
        type = note_data.get('type', '未知类型')
        time_str = time.strftime('%Y-%m-%d %H:%M:%S')
        # 构造通知内容
        content = [
            "小红书用户发布新笔记",
            f"用户：{user_name}",
            f"标题：{title}",
            f"链接：{note_url}",
            f"类型：{type}",
        ]
        
        if interact_result and MONITOR_CONFIG.get("AUTO_INTERACT"):
            like_status = "成功" if interact_result["like_status"] else "失败"
            content.append(f"点赞：{like_status}")
            
            if interact_result["comment_status"]:
                content.append(f"评论：成功")
                content.append(f"评论内容：{interact_result['comment_content']}")
            else:
                content.append(f"评论：失败")
        
        content.append(f"监控时间：{time_str}")
        
        self.wecom.send_text("\n".join(content))

    def monitor_multiple_users(self, user_ids: List[str], interval: int):
        """
        同时监控多个用户动态
        :param user_ids: 用户ID列表
        :param interval: 检查间隔(秒)
        """
        print(f"开始监控多个用户: {user_ids}")

        # 提交每个用户的监控任务到线程池
        futures = [self.executor.submit(self.monitor_user, user_id, interval) for user_id in user_ids]

        # 等待所有任务完成
        for future in futures:
            future.result()  # 如果任务失败，会抛出异常

    def monitor_user(self, user_id: str, interval: int):
        """
        监控单个用户动态
        :param user_id: 用户ID
        :param interval: 检查间隔(秒)
        """
        print(f"开始监控用户: {user_id}")

        while True:
            try:
                latest_notes = self.get_latest_notes(user_id)

                # 使用锁来保护数据库操作
                with self.lock:
                    existing_notes = self.db.get_user_notes_count(user_id)
                    is_first_monitor = existing_notes == 0 and len(latest_notes) > 1

                    if is_first_monitor:
                        welcome_msg = (
                            f"欢迎使用小红书用户监控系统\n"
                            f"监控用户：{latest_notes[0].get('user', {}).get('nickname', user_id)}\n"
                            f"首次监控某用户时，不会对历史笔记进行自动点赞和评论，仅保存笔记记录\n"
                            f"以防止被系统以及用户发现"
                        )
                        self.wecom.send_text(welcome_msg)

                        for note in latest_notes:
                            self.db.add_note_if_not_exists(note)
                    else:
                        for note in latest_notes:
                            with self.lock:
                                if self.db.add_note_if_not_exists(note):
                                    print(f"发现新笔记: {note.get('display_title')}")
                                    interact_result = self.interact_with_note(note)
                                    self.send_note_notification(note, interact_result)

            except Exception as e:
                error_msg = str(e)
                print(f"监控过程发生错误: {error_msg}")

            time.sleep(interval)


def main():
    monitor = XHSMonitor(
        cookie=XHS_CONFIG["COOKIE"],
        webhook_url=WECOM_CONFIG["WEBHOOK_URL"]
    )

    # 假设你要监控5个用户
    user_ids = [
        MONITOR_CONFIG["USER_ID_1"],
        MONITOR_CONFIG["USER_ID_2"],
        MONITOR_CONFIG["USER_ID_3"],
        MONITOR_CONFIG["USER_ID_4"],
        MONITOR_CONFIG["USER_ID_5"]
    ]

    monitor.monitor_multiple_users(
        user_ids=user_ids,
        interval=MONITOR_CONFIG["CHECK_INTERVAL"]
    )


if __name__ == "__main__":
    main()
