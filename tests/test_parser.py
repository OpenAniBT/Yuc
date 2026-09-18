import unittest

from yuc.parser import parse_page, text
from bs4 import BeautifulSoup


def page(body, title="2026年10月新番表"):
    return (f'<html><head><meta property="og:title" content="{title}"></head>'
            f'<body><article><h1 class="post-title">{title}</h1>'
            f'<div class="post-body" itemprop="articleBody">{body}</div>'
            '</article></body></html>').encode()


def source(slug="202610", kind="quarter"):
    return {"url": f"https://yuc.wiki/{slug}/", "pageId": slug, "pageType": kind,
            "quarterCode": slug if kind == "quarter" else None,
            "rawHtmlPath": f"raw/{slug}.html", "retrievedAt": "2026-09-18T00:00:00+00:00",
            "httpStatus": 200, "etag": None, "lastModified": None, "contentType": "text/html; charset=utf-8",
            "transport": {"tlsVerified": True, "route": "dns", "connectAddress": None}}


CARD = '''<!--#C19--><div><img data-src="//img.example/cover.jpg" src="placeholder.png"></div>
<div><table><tr><td class="title_main_r"><p class="title_cn_r3">魔法★探险家<br>转生为成人游戏万年男二又怎样<br>我要活用游戏知识自由生活</p>
<p class="title_jp_r2">マジカル★エクスプローラー<br>エロゲの友人キャラに転生したけど、<br>ゲーム知識使って自由に生きる</p></td>
<td class="type_c_r">小说改编动画</td></tr><tr><td class="type_tag_r">转生/奇幻/恋爱<br>喜剧/后宫/福利</td></tr>
<tr><td class="staff_r">原作：入栖 (角川)<br>插画：神奈月升 / 漫画：绯贺Yukari<br>
导演：大桥一辉<br>编剧：关根聪子<br>动画人设：木宫亮介<br>音乐：末广健一郎<br>动画制作：WHITE FOX</td>
<td class="cast_r">岛崎信长<br>加隈亚衣<br>大西沙织<br>M·A·O<br>渕上舞<br>津田美波<br>纺木吏佐<br>结川麻希<br>远野光</td>
<td class="link_a_r"><a href="https://majieku.com/">动画官网</a><a href="https://www.bilibili.com/video/BV1SoXxB9EZv/">PV</a>
<p class="broadcast_r">10/3周六深夜</p><p class="broadcast_ex_r">连续两话</p></td></tr>
<tr><td class="link_b_r"><a href="https://example.org/watch"><img src="/platform.png">港台</a></td></tr>
<tr><td class="new-layout-field">未来未知字段也不能丢</td></tr></table></div>'''


class ParserTests(unittest.TestCase):
    def test_complete_introduction_without_lineup(self):
        document = parse_page(page(CARD), source())
        item = document["introductions"][0]
        self.assertEqual(document["lineup"], [])
        self.assertEqual(len(item["titles"]["chinese"]["lines"]), 3)
        self.assertEqual(len(item["cast"]["lines"]), 9)
        self.assertIn("M·A·O", item["cast"]["lines"])
        self.assertEqual(item["adaptation"], "小说改编动画")
        self.assertEqual(item["tags"]["values"], ["转生", "奇幻", "恋爱", "喜剧", "后宫", "福利"])
        self.assertEqual(len(item["staff"]["credits"]), 8)
        self.assertEqual(item["studios"], ["WHITE FOX"])
        self.assertEqual(item["cover"]["url"], "https://img.example/cover.jpg")
        self.assertEqual(item["officialSites"][0]["url"], "https://majieku.com/")
        self.assertEqual(item["promotionalVideos"][0]["url"], "https://www.bilibili.com/video/BV1SoXxB9EZv/")
        self.assertEqual(item["broadcast"]["text"], "10/3周六深夜")
        self.assertEqual(item["broadcastNotes"]["text"], "连续两话")
        self.assertEqual(item["platforms"][0]["label"], "港台")
        self.assertIn("未来未知字段也不能丢", item["raw"]["text"])
        self.assertIn("未来未知字段也不能丢", document["content"]["html"])

    def test_old_classes_and_repeated_source_marker(self):
        document = parse_page(page(CARD.replace("_r", "") * 2), source())
        a, b = document["introductions"]
        self.assertNotEqual(a["recordId"], b["recordId"])
        self.assertEqual(a["sourceMarker"], "#C19")
        self.assertEqual(b["sourceMarker"], "#C19")
        self.assertEqual(a["adaptation"], "小说改编动画")

    def test_missing_episode_time_is_not_shifted_to_status(self):
        body = '''<table><tr><td class="date2">网络放送 &amp; 其他</td></tr></table>
<div style="float:left"><div class="div_date_"><img data-src="https://img.example/a.jpg"></div>
<div><table><tr><td class="date_title__">人造人009</td></tr>
<tr class="tr_area_ex"><td><p class="paomian">(全3话)</p></td></tr></table></div></div>'''
        item = parse_page(page(body), source())["lineup"][0]
        self.assertEqual(item["statusOrTime"], [])
        self.assertEqual(item["episodeNotes"], ["(全3话)"])
        self.assertEqual(item["group"], "网络放送 & 其他")
        self.assertEqual(item["cover"]["url"], "https://img.example/a.jpg")

    def test_weekday_and_25_hour_time_retained(self):
        body = '''<table><tr><td class="date2">周一 (月)</td></tr></table>
<div><div class="div_date"><p>25:00~</p><p>(全12话)</p></div><div><table><tr><td class="date_title_">节目</td></tr>
<tr class="tr_area"><td><a href="https://www.netflix.com/title/123"><p class="area">环大陆</p></a></td></tr></table></div></div>'''
        item = parse_page(page(body), source())["lineup"][0]
        self.assertEqual(item["statusOrTime"], ["25:00~"])
        self.assertEqual(item["platforms"][0]["label"], "环大陆")
        self.assertEqual(item["platforms"][0]["host"], "www.netflix.com")
        legacy = parse_page(page(body.replace('class="date2"', 'class="date"')), source())["lineup"][0]
        self.assertEqual(legacy["group"], "周一 (月)")

    def test_compact_cards_include_sibling_tables(self):
        body = '''<div><div class="future_div"><p class="future_type_c">小说</p><p class="future_date">2027</p><img src="/a.jpg"></div>
<div><table class="future_table"><tr><td class="future_title_">未来节目</td></tr></table></div></div>
<div><div class="div_sp"><img src="/b.jpg"></div><div><table><tr><td class="sp_title">特别篇</td></tr>
<tr><td class="type-o">OVA</td><td class="sp_release">未定</td></tr></table></div></div>
<div><div class="div_sp"><img src="/c.jpg"></div><div><table><tr><td class="movie_title">电影</td></tr>
<tr><td class="type-o">延期</td><td class="movie_release">原定2025/12/6</td></tr><tr><td class="movie_box">票房原文</td></tr></table></div></div>'''
        document = parse_page(page(body), source("new", "article"))
        self.assertEqual(document["previews"][0]["title"]["text"], "未来节目")
        self.assertEqual(document["previews"][0]["release"]["text"], "2027")
        self.assertEqual(document["specials"][0]["format"]["text"], "OVA")
        self.assertEqual(document["movies"][0]["boxOffice"]["text"], "票房原文")

    def test_inline_text_does_not_gain_spurious_newline(self):
        node = BeautifulSoup('<p>原作：<a href="/">入栖</a> (角川)<br>下一行</p>', "html.parser").p
        self.assertEqual(text(node), "原作：入栖 (角川)\n下一行")

    def test_error_page_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing article body"):
            parse_page(b'<html><h1>Access denied</h1></html>', source())

    def test_metadata_retains_repeated_and_unnamed_tags(self):
        payload = page(CARD).replace(b'</head>', b'<meta content="unnamed"><meta property="og:image" content="a"><meta property="og:image" content="b"></head>')
        document = parse_page(payload, source())
        self.assertIn({'content': 'unnamed'}, document['metadata'])
        self.assertEqual([m['content'] for m in document['metadata'] if m.get('property') == 'og:image'], ['a', 'b'])


if __name__ == "__main__":
    unittest.main()
