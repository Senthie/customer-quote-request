"""
Author: '浪川' '1214391613@qq.com'
Date: 2026-03-31 09:58:51
LastEditors: '浪川' '1214391613@qq.com'
LastEditTime: 2026-04-10 11:30:06
FilePath: /customer-quote-request-0.0.10/scripts/uuid_utils.py
Description: uuid_utils.py - 生成任务 UUID
"""

from uuid import uuid4


def get_work_uuid():
    """
    生成并打印一个 UUID4
    """
    new_uuid = uuid4().hex
    print(new_uuid)
    return new_uuid


if __name__ == "__main__":
    get_work_uuid()
