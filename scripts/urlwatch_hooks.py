"""urlwatch の hooks(--hooks で読み込む)。

botguard フィルター: 取得した本文に「bot対策の確認ページ」の目印が入っていたら、そのジョブを「エラー」にする。
エラーにすると urlwatch は保存済みの内容を更新しないため、
  ・確認ページへの変わり、確認ページから元のページへの戻り、という2回の「変更検知」が出ません。
  ・スクリプト側が、全部の確認が終わってから取り直せます(urlwatch_notify.py の再試行)。
目印は common.yaml の web.bot_challenge.markers(正規表現の候補)。生成時に urls.yaml へ書き込まれます。
"""
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
