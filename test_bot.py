"""单元测试（标准库 unittest，零第三方依赖）：python test_bot.py -v
覆盖纯逻辑：md5 提示提取、DB 状态机与检索、磁力表头匹配与解析链路。
全部使用临时目录，不碰真实数据。"""
import asyncio
import hashlib
import json
import logging
import re
import tempfile
import time
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

from config import load_config
from db import DB, MAX_DL_TRIES, MAX_EVENT_TRIES
from storage import Storage, md5_hint, is_sticker
from magnets import MagnetRegistry
from bot import Bot

H1 = "a" * 32
H2 = "b" * 32


class TestMd5Hint(unittest.TestCase):
    def test_hash_filename(self):
        self.assertEqual(md5_hint(H1 + ".jpg"), H1)
        self.assertEqual(md5_hint(H1.upper() + ".PNG"), H1)

    def test_non_md5(self):
        self.assertIsNone(md5_hint("{67000A75-DE31-880E-308B-211C3794F31B}.png"))
        self.assertIsNone(md5_hint("abc.jpg"))
        self.assertIsNone(md5_hint(None))
        self.assertIsNone(md5_hint(""))


class TestSticker(unittest.IsolatedAsyncioTestCase):
    def test_is_sticker(self):
        self.assertTrue(is_sticker({"sub_type": 1, "file": "a.jpg"}))
        self.assertTrue(is_sticker({"sub_type": "1", "file": "a.jpg"}))
        self.assertTrue(is_sticker({"emoji_id": "x", "file": "a.jpg"}))
        self.assertTrue(is_sticker({"file": "a.gif"}))                 # GIF 一律表情包
        self.assertTrue(is_sticker({"file": "a.gif", "sub_type": 0}))
        self.assertFalse(is_sticker({"file": "a.jpg", "sub_type": 0}))  # 照片
        self.assertFalse(is_sticker({"file": "b.png", "sub_type": 0}))  # 截图
        self.assertFalse(is_sticker({}))

    async def test_sticker_not_downloaded(self):
        tmp = Path(tempfile.mkdtemp())
        db = DB(tmp / "t.db")
        bot = Bot({"data_dir": str(tmp), "ws_url": "ws://x",
                   "api_url": "http://x", "ws_token": ""}, None, db, None)
        t = int(time.time())
        sticker = {"message_id": "s1", "time": t, "post_type": "message",
                   "message_type": "group", "group_id": 9, "user_id": 8,
                   "sender": {"user_id": 8, "nickname": "甲"},
                   "message": [{"type": "image",
                                "data": {"file": "x.gif", "sub_type": 1,
                                         "url": "http://127.0.0.1:1/x.gif"}}]}
        await bot._ingest(json.dumps(sticker), sticker)
        while not bot.q.empty():
            eid, data = await bot.q.get()
            await bot._run_event_safe(eid, data)
        rows = await db._run(lambda c: c.execute(
            "SELECT kind, text, dl, src FROM messages WHERE msg_id='s1'"
        ).fetchall())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "sticker")
        self.assertEqual(rows[0]["text"], "[表情包]")
        self.assertEqual(rows[0]["dl"], "none")     # 没进下载队列
        self.assertEqual(rows[0]["src"], None)


class TestDB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = DB(self.tmp / "t.db")

    async def asyncTearDown(self):
        pass

    async def test_event_lifecycle(self):
        eid = await self.db.add_event('{"x":1}')
        self.assertTrue(await self.db.claim_event(eid))
        self.assertFalse(await self.db.claim_event(eid))   # busy 不可再认领
        await self.db.finish_event(eid)
        self.assertEqual((await self.db.event_stats()).get("done"), 1)

    async def test_event_fail_backoff_to_dead(self):
        eid = await self.db.add_event("{}")
        await self.db.claim_event(eid)
        for i in range(MAX_EVENT_TRIES):
            status = await self.db.fail_event(eid, "boom")
        self.assertEqual(status, "dead")

    async def test_prune_keeps_errors_and_recent(self):
        old = time.time() - 31 * 86400
        await self.db._run(lambda c: c.executemany(
            "INSERT INTO events(received_at, status, payload) VALUES(?,?,?)",
            [(old, "done", "old-done"), (old, "error", "old-error"),
             (time.time(), "done", "new-done")]))
        n = await self.db.prune_events()
        self.assertEqual(n, 1)
        stats = await self.db.event_stats()
        self.assertEqual(stats.get("error"), 1)
        self.assertEqual(stats.get("done"), 1)

    async def test_fts_and_like_search(self):
        await self.db.add_message(ts=int(time.time()), scope="group",
                                  text="今天天气真的不错")
        await self.db.add_message(ts=int(time.time()), scope="private",
                                  text=r"路径 C:\dir\file 记录")
        self.assertEqual(len(await self.db.search("天气真的")), 1)  # trigram 连续子串
        self.assertEqual(len(await self.db.search("天气")), 1)      # <3 字回退 LIKE
        self.assertEqual(len(await self.db.search(r"C:\dir")), 1)   # 含反斜杠

    async def test_dl_bump_to_dead_and_recheck(self):
        mid = await self.db.add_message(ts=int(time.time()), kind="video",
                                        dl="pending")
        for i in range(MAX_DL_TRIES):
            status = await self.db.bump_dl(mid, "download failed")
        self.assertEqual(status, "dead")
        # dead 行改到期后应能被认领（每天复检机制）
        await self.db._run(lambda c: c.execute(
            "UPDATE messages SET next_try=0 WHERE id=?", (mid,)))
        rows = await self.db.claim_pending_dl()
        self.assertEqual([r["id"] for r in rows], [mid])

    async def test_msg_known_dedup(self):
        await self.db.add_message(ts=100, scope="group", peer_id=5,
                                  sender_id=7, msg_id="m1", text="hi")
        self.assertTrue(await self.db.msg_known("m1", "group", 5, 7, 999))
        self.assertTrue(await self.db.msg_known("", "group", 5, 7, 100))  # 老数据兜底
        self.assertFalse(await self.db.msg_known("m2", "group", 5, 7, 101))
        self.assertFalse(await self.db.msg_known("", "private", 5, 7, 100))

    async def test_active_chats(self):
        now = int(time.time())
        await self.db.add_message(ts=now, scope="group", peer_id=1, text="a")
        await self.db.add_message(ts=now - 86400, scope="private", peer_id=2, text="b")
        await self.db.add_message(ts=now - 30 * 86400, scope="group", peer_id=3, text="c")
        chats = await self.db.active_chats(14)
        self.assertEqual([(c["scope"], c["peer_id"]) for c in chats],
                         [("group", 1), ("private", 2)])


class TestMagnet(unittest.TestCase):
    def setUp(self):
        self.reg = MagnetRegistry(Path("unused.xlsx"), object())

    def test_cols_by_header_text(self):
        cols = MagnetRegistry._cols(
            ["序号", "特征码(btih)", "资源名称(dn)", "标题", "大小",
             "出现次数", "首次日期", "磁力链接"])
        self.assertEqual(cols["seq"], 0)
        self.assertEqual(cols["hash"], 1)
        self.assertEqual(cols["dn"], 2)
        self.assertEqual(cols["count"], 5)
        self.assertEqual(cols["link"], 7)

    def test_base_seq(self):
        self.assertEqual(MagnetRegistry._base_seq({1, 5}, 10), 5)
        self.assertEqual(MagnetRegistry._base_seq(set(), 10), 10)

    def test_rows_for_parse_and_dedup(self):
        class FakeRules:
            MAGNET_RAW = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]+")
            SIZE_RE = re.compile(r"([\d.]+)\s*(GB|MB|KB)", re.I)

            @staticmethod
            def usable_title(t):
                return bool(t.strip())

            @staticmethod
            def parse_magnet(m):
                h = m.split("btih:")[1].lower()
                return m, h, "real", "", ""

        self.reg.rules = FakeRules()
        text = "数学课件 magnet:?xt=urn:btih:" + "C" * 32 + " 1.5GB"
        rows = self.reg._rows_for(text, "group", 12345, "张三", 1700000000)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["hash"], "c" * 32)
        self.assertEqual(r["title"], "数学课件")
        self.assertEqual(r["size"], "1.5 GB")
        self.assertEqual(r["gid"], "12345")
        self.assertEqual(r["sender"], "张三")
        # 同 btih 再来不重复登记
        self.assertEqual(self.reg._rows_for(text, "group", 1, "x", 0), [])


class TestStorage(unittest.IsolatedAsyncioTestCase):
    async def test_disk_hit_then_seen_and_register(self):
        tmp = Path(tempfile.mkdtemp())
        db = DB(tmp / "t.db")
        st = Storage(tmp, None, db=db)
        img = st.priv_img / (H1 + ".jpg")
        img.write_bytes(b"hello")
        r1 = await st.save_media("http://127.0.0.1:1/x.jpg", "image",
                                 "private", expect_md5=H1)
        self.assertEqual(r1["path"], str(img))
        self.assertIn(H1, st.seen)
        # 去重复用也要登记 media 表：合并转发路径保存的媒体没有 messages 行，
        # 删后免拉全靠这张表
        row = await db._run(lambda c: c.execute(
            "SELECT md5, path FROM media WHERE md5=?", (H1,)).fetchone())
        self.assertIsNotNone(row)
        self.assertEqual(row["path"], str(img))
        img.unlink()   # 模拟用户删文件：seen 命中依旧免拉
        r2 = await st.save_media("http://127.0.0.1:1/x.jpg", "image",
                                 "group", expect_md5=H1)
        self.assertEqual(r2["path"], str(img))

    async def test_video_goes_to_unified_dir(self):
        tmp = Path(tempfile.mkdtemp())
        st = Storage(tmp, None)
        v = st.vid_all / (H2 + ".mp4")
        v.write_bytes(b"v")
        r = await st.save_media("http://127.0.0.1:1/x.mp4", "video",
                                "group", expect_md5=H2)
        self.assertEqual(r["path"], str(v))

    async def test_local_source_gets_placed(self):
        # 回归测试：流式改造曾漏掉本地来源的落盘分支
        tmp = Path(tempfile.mkdtemp())
        db = DB(tmp / "t.db")
        st = Storage(tmp, None, db=db)
        src = tmp / "napcat_cache.mp4"
        src.write_bytes(b"original-bytes")
        r = await st.save_media(str(src), "video", "group")
        dest = st.vid_all / (hashlib.md5(b"original-bytes").hexdigest() + ".mp4")
        self.assertEqual(r["path"], str(dest))
        self.assertTrue(dest.is_file())          # 文件必须真的被放进去
        self.assertEqual(dest.read_bytes(), b"original-bytes")
        self.assertTrue(src.is_file())           # 不动 NapCat 原件
        row = await db._run(lambda c: c.execute(
            "SELECT path FROM media WHERE md5=?", (r["md5"],)).fetchone())
        self.assertIsNotNone(row)
        # 同内容再来：盘上命中免复制
        r2 = await st.save_media(str(src), "video", "private")
        self.assertEqual(r2["path"], str(dest))

    async def test_save_file_local_source(self):
        tmp = Path(tempfile.mkdtemp())
        st = Storage(tmp, None)
        src = tmp / "report.pdf"
        src.write_bytes(b"%PDF-fake")
        r = await st.save_file(str(src), "report.pdf", None, scope="group")
        self.assertIsNotNone(r)
        dest = st.grp_file / (hashlib.md5(b"%PDF-fake").hexdigest() + ".pdf")
        self.assertEqual(Path(r["path"]), dest)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"%PDF-fake")


class TestBackfill(unittest.IsolatedAsyncioTestCase):
    def _mk_msg(self, mid, t, text="x"):
        return {"message_id": mid, "time": t, "post_type": "message",
                "message_type": "group", "group_id": 9,
                "user_id": 8, "sender": {"user_id": 8, "nickname": "甲"},
                "message": [{"type": "text", "data": {"text": text}}]}

    async def test_backfill_chat_end_to_end(self):
        tmp = Path(tempfile.mkdtemp())
        db = DB(tmp / "t.db")
        bot = Bot({"data_dir": str(tmp), "ws_url": "ws://x",
                   "api_url": "http://x", "ws_token": ""}, None, db, None)
        # 一条已归档的老消息（含 msg_id）+ 两条新消息
        t0 = int(time.time()) - 3600
        await db.add_message(ts=t0, scope="group", peer_id=9, sender_id=8,
                             msg_id="old1", text="old")
        calls = []

        async def fake_api(action, params, timeout=15):
            calls.append((action, params))
            self.assertEqual(action, "get_group_msg_history")
            return {"messages": [self._mk_msg("old1", t0, "old"),
                                 self._mk_msg("new1", t0 + 10, "hello"),
                                 self._mk_msg("new2", t0 + 20, "world")]}

        bot.call_api = fake_api
        added = await bot._backfill_chat("group", 9)
        self.assertEqual(added, 2)
        # 消息进了队列，跑一遍管道后落库且带 msg_id
        while not bot.q.empty():
            eid, data = await bot.q.get()
            await bot._run_event_safe(eid, data)
        rows = await db._run(lambda c: c.execute(
            "SELECT msg_id, text FROM messages WHERE msg_id IN ('new1','new2') "
            "ORDER BY seg_index").fetchall())
        self.assertEqual([r["msg_id"] for r in rows], ["new1", "new2"])
        self.assertEqual(rows[0]["text"], "hello")
        # 再拉一轮：全部已知，0 补录
        self.assertEqual(await bot._backfill_chat("group", 9), 0)

    async def test_backfill_streak_stop(self):
        tmp = Path(tempfile.mkdtemp())
        db = DB(tmp / "t.db")
        bot = Bot({"data_dir": str(tmp), "ws_url": "ws://x",
                   "api_url": "http://x", "ws_token": ""}, None, db, None)
        t0 = int(time.time()) - 3600
        # 40 条已知的旧消息（超过 KNOWN_STREAK=30 触发提前停）
        for i in range(40):
            await db.add_message(ts=t0 + i, scope="group", peer_id=9,
                                 sender_id=8, msg_id=f"k{i}", text=f"k{i}")
        msgs = [self._mk_msg(f"k{i}", t0 + i, f"k{i}") for i in range(40)]
        msgs.append(self._mk_msg("brand-new", t0 + 100, "fresh"))
        pages = []

        async def fake_api(action, params, timeout=15):
            pages.append(dict(params))
            return {"messages": msgs}

        bot.call_api = fake_api
        added = await bot._backfill_chat("group", 9)
        self.assertEqual(added, 1)
        # 连续 30 条已知后停，不再翻页
        self.assertEqual(len(pages), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
