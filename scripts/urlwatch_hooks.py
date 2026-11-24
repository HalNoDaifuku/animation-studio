"""urlwatch の hooks(--hooks で読み込む)。

botguard フィルター: 取得した本文に「bot対策の確認ページ」の目印が入っていたら、そのジョブを「エラー」にする。
エラーにすると urlwatch は保存済みの内容を更新しないため、
  ・確認ページへの変わり、確認ページから元のページへの戻り、という2回の「変更検知」が出ません。
  ・スクリプト側が、全部の確認が終わってから取り直せます(urlwatch_notify.py の再試行)。
目印は common.yaml の web.bot_challenge.markers(正規表現の候補)。生成時に urls.yaml へ書き込まれます。
"""
import hashlib
import json
import os
import re

from urlwatch import filters


class BotChallengeGuard(filters.FilterBase):
    """botguard: 本文に引数の正規表現(| で複数)が見つかったら例外にする。"""

    __kind__ = "botguard"
    __no_subfilter__ = False

    def filter(self, data, subfilter=None):
        if subfilter and re.search(subfilter, data):
            raise ValueError("bot対策の確認ページが返ってきました(取得し直します)")
        return data


class ContentHash(filters.FilterBase):
    """contenthash: フィルターを通したあとの本文のハッシュを、環境変数 WATCH_HASH_FILE のファイルへ1行(JSON)で追記する。

    「見たことのある内容への戻り」を通知しないための記録用(本文は変えない)。いちばん最後のフィルターとして付く。
    """

    __kind__ = "contenthash"
    __no_subfilter__ = True

    def filter(self, data, subfilter=None):
        path = os.environ.get("WATCH_HASH_FILE")
        name = getattr(self.job, "name", None)
        if path and name and isinstance(data, str):
            digest = hashlib.sha256(data.encode("utf-8", "replace")).hexdigest()[:20]
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"name": name, "hash": digest}, ensure_ascii=False) + "\n")
        return data
