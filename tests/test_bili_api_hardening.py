"""B站搜索加固（Wbi 签名与分页完整性）的回归测试。"""

import unittest
from unittest.mock import AsyncMock

import nonebot

nonebot.init()

from src.plugins.group_request_manager.bili_api import (
    NICKNAME_NOT_FOUND_MESSAGE,
    SEARCH_INCOMPLETE_MESSAGE,
    BiliApi,
    _get_mixin_key,
    _wbi_sign_query,
    reset_wbi_key_cache,
)

# 官方文档示例密钥（bilibili-API-collect docs/misc/sign/wbi.md），作为签名实现的黄金基准
DOC_IMG_KEY = "7cd084941338484aae1ad9425b84077c"
DOC_SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
DOC_MIXIN_KEY = "ea1db124af3c7062474693fa704f4ff8"

NAV_RESPONSE = {
    "code": -101,
    "message": "账号未登录",
    "data": {
        "isLogin": False,
        "wbi_img": {
            "img_url": f"https://i0.hdslb.com/bfs/wbi/{DOC_IMG_KEY}.png",
            "sub_url": f"https://i0.hdslb.com/bfs/wbi/{DOC_SUB_KEY}.png",
        },
    },
}


def search_payload(result, num_results=None):
    data = {"result": result}
    if num_results is not None:
        data["numResults"] = num_results
    return {"code": 0, "data": data}


class WbiSignTests(unittest.TestCase):
    def test_mixin_key_matches_official_example(self):
        self.assertEqual(_get_mixin_key(DOC_IMG_KEY, DOC_SUB_KEY), DOC_MIXIN_KEY)

    def test_sign_matches_official_example(self):
        query = _wbi_sign_query(
            {"foo": "114", "bar": "514", "zab": 1919810},
            DOC_IMG_KEY,
            DOC_SUB_KEY,
            wts=1702204169,
        )
        self.assertIn("wts=1702204169", query)
        self.assertIn("w_rid=8f6f2b5b3d485fe1886cec6a0be8c5d4", query)

    def test_sign_encodes_uppercase_and_spaces(self):
        query = _wbi_sign_query({"foo": "one one four"}, DOC_IMG_KEY, DOC_SUB_KEY, wts=1)
        self.assertIn("foo=one%20one%20four", query)
        self.assertNotIn("+", query)

    def test_sign_is_deterministic(self):
        params = {"search_type": "bili_user", "keyword": "测试昵称"}
        first = _wbi_sign_query(params, DOC_IMG_KEY, DOC_SUB_KEY, wts=42)
        second = _wbi_sign_query(params, DOC_IMG_KEY, DOC_SUB_KEY, wts=42)
        self.assertEqual(first, second)


class SearchWbiRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_wbi_key_cache()

    def tearDown(self):
        reset_wbi_key_cache()

    async def test_search_uses_wbi_signed_request(self):
        api = BiliApi()
        api._get = AsyncMock(side_effect=[NAV_RESPONSE, search_payload([], 0)])
        try:
            result = await api.search_user_profile("someone")
        finally:
            await api.close()

        self.assertEqual(result.state, "failed")
        nav_call, search_call = api._get.await_args_list[:2]
        self.assertIn("/x/web-interface/nav", nav_call.args[0])
        self.assertIn("/x/web-interface/wbi/search/type", search_call.args[0])
        self.assertIsInstance(search_call.args[1], str)
        self.assertIn("w_rid=", search_call.args[1])
        self.assertIn("wts=", search_call.args[1])

    async def test_wbi_keys_are_cached_across_searches(self):
        api = BiliApi()
        api._get = AsyncMock(
            side_effect=[NAV_RESPONSE, search_payload([], 0), search_payload([], 0)]
        )
        try:
            await api.search_user_profile("someone")
            await api.search_user_profile("someone")
        finally:
            await api.close()
        # nav 只在第一次搜索时请求一次
        self.assertEqual(api._get.await_count, 3)

    async def test_nav_failure_falls_back_to_unsigned_search(self):
        api = BiliApi()
        api._get = AsyncMock(
            side_effect=[RuntimeError("nav offline"), search_payload([], 0)]
        )
        try:
            result = await api.search_user_profile("someone")
        finally:
            await api.close()

        self.assertEqual(result.state, "failed")
        search_call = api._get.await_args_list[1]
        self.assertIn("/x/web-interface/search/type", search_call.args[0])
        self.assertIsInstance(search_call.args[1], dict)

    async def test_malformed_nav_response_falls_back_to_unsigned_search(self):
        api = BiliApi()
        api._get = AsyncMock(
            side_effect=[{"code": 0, "data": {"isLogin": True}}, search_payload([], 0)]
        )
        try:
            result = await api.search_user_profile("someone")
        finally:
            await api.close()

        self.assertEqual(result.state, "failed")
        search_call = api._get.await_args_list[1]
        self.assertIn("/x/web-interface/search/type", search_call.args[0])


class SearchCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def search_once(self, payload):
        api = BiliApi()
        api._get = AsyncMock(return_value=payload)
        try:
            return await api.search_user_profile("昵称")
        finally:
            await api.close()

    async def test_no_results_complete_is_failed(self):
        result = await self.search_once(search_payload([], 0))
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.detail, NICKNAME_NOT_FOUND_MESSAGE)

    async def test_fuzzy_only_complete_is_failed(self):
        result = await self.search_once(
            search_payload([{"mid": "1", "uname": "相似昵称", "level": 3}], 1)
        )
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.detail, NICKNAME_NOT_FOUND_MESSAGE)

    async def test_truncated_results_without_exact_match_is_inaccessible(self):
        result = await self.search_once(
            search_payload([{"mid": "1", "uname": "相似昵称", "level": 3}], 50)
        )
        self.assertEqual(result.state, "inaccessible")
        self.assertEqual(result.detail, SEARCH_INCOMPLETE_MESSAGE)

    async def test_missing_numresults_without_exact_match_is_inaccessible(self):
        result = await self.search_once(
            search_payload([{"mid": "1", "uname": "相似昵称", "level": 3}])
        )
        self.assertEqual(result.state, "inaccessible")
        self.assertEqual(result.detail, SEARCH_INCOMPLETE_MESSAGE)

    async def test_invalid_numresults_is_inaccessible(self):
        result = await self.search_once(search_payload([], "not-a-number"))
        self.assertEqual(result.state, "inaccessible")

    async def test_unique_exact_match_is_passed_even_when_truncated(self):
        result = await self.search_once(
            search_payload(
                [
                    {"mid": "9", "uname": "昵称", "level": 5},
                    {"mid": "1", "uname": "相似昵称", "level": 3},
                ],
                88,
            )
        )
        self.assertEqual(result.state, "passed")
        self.assertEqual(result.matched_target_uid, 9)
        self.assertEqual(result.matched_level, 5)

    async def test_multiple_exact_matches_still_failed_when_truncated(self):
        result = await self.search_once(
            search_payload(
                [
                    {"mid": "9", "uname": "昵称", "level": 5},
                    {"mid": "8", "uname": "昵称", "level": 3},
                ],
                88,
            )
        )
        self.assertEqual(result.state, "failed")
        self.assertIn("多个同名", result.detail)


if __name__ == "__main__":
    unittest.main()
