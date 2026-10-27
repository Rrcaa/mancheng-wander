"""内置样例 POI。

仅在未配置高德 Key 时兜底使用，保证零配置即可跑出真实感行程。
字段：name / category / address / lon / lat / visit(建议停留分钟) / rating / tags。
真实部署请配置 AMAP_API_KEY，由 geo.AmapProvider 替换本数据。
"""
from __future__ import annotations

from typing import Any

CITIES: dict[str, dict[str, Any]] = {
    "北京": {
        "center": (116.397, 39.908),
        "pois": [
            {"name": "故宫博物院", "category": "历史", "address": "东城区景山前街4号", "lon": 116.397, "lat": 39.917, "visit": 180, "rating": 4.8, "tags": ["历史", "博物馆", "古迹"]},
            {"name": "颐和园", "category": "历史", "address": "海淀区新建宫门路19号", "lon": 116.275, "lat": 39.999, "visit": 150, "rating": 4.7, "tags": ["历史", "园林", "自然"]},
            {"name": "八达岭长城", "category": "历史", "address": "延庆区八达岭镇", "lon": 116.017, "lat": 40.359, "visit": 180, "rating": 4.7, "tags": ["历史", "古迹", "自然"]},
            {"name": "天坛公园", "category": "宗教", "address": "东城区天坛内东里", "lon": 116.411, "lat": 39.881, "visit": 90, "rating": 4.6, "tags": ["宗教", "历史", "公园"]},
            {"name": "南锣鼓巷", "category": "美食", "address": "东城区南锣鼓巷", "lon": 116.403, "lat": 39.937, "visit": 90, "rating": 4.3, "tags": ["美食", "小吃", "购物"]},
            {"name": "三里屯太古里", "category": "现代", "address": "朝阳区三里屯路19号", "lon": 116.454, "lat": 39.936, "visit": 90, "rating": 4.4, "tags": ["现代", "购物", "夜景"]},
            {"name": "国家博物馆", "category": "历史", "address": "东城区东长安街16号", "lon": 116.397, "lat": 39.904, "visit": 150, "rating": 4.8, "tags": ["历史", "博物馆"]},
            {"name": "什刹海", "category": "夜景", "address": "西城区羊房胡同", "lon": 116.383, "lat": 39.941, "visit": 90, "rating": 4.5, "tags": ["夜景", "美食", "自然"]},
        ],
    },
    "上海": {
        "center": (121.473, 31.230),
        "pois": [
            {"name": "外滩", "category": "现代", "address": "黄浦区中山东一路", "lon": 121.490, "lat": 31.245, "visit": 90, "rating": 4.7, "tags": ["现代", "夜景", "江景"]},
            {"name": "豫园", "category": "历史", "address": "黄浦区安仁街137号", "lon": 121.492, "lat": 31.228, "visit": 120, "rating": 4.5, "tags": ["历史", "园林", "古迹"]},
            {"name": "上海博物馆", "category": "历史", "address": "黄浦区人民大道201号", "lon": 121.473, "lat": 31.231, "visit": 150, "rating": 4.8, "tags": ["历史", "博物馆"]},
            {"name": "静安寺", "category": "宗教", "address": "静安区南京西路1686号", "lon": 121.445, "lat": 31.224, "visit": 60, "rating": 4.5, "tags": ["宗教", "历史"]},
            {"name": "南京路步行街", "category": "美食", "address": "黄浦区南京东路", "lon": 121.480, "lat": 31.236, "visit": 90, "rating": 4.4, "tags": ["美食", "购物", "小吃"]},
            {"name": "迪士尼乐园", "category": "亲子", "address": "浦东新区川沙新镇", "lon": 121.670, "lat": 31.150, "visit": 240, "rating": 4.6, "tags": ["亲子", "现代"]},
            {"name": "城隍庙", "category": "美食", "address": "黄浦区方浜中路249号", "lon": 121.491, "lat": 31.227, "visit": 60, "rating": 4.3, "tags": ["美食", "小吃", "历史"]},
            {"name": "陆家嘴", "category": "现代", "address": "浦东新区世纪大道", "lon": 121.506, "lat": 31.239, "visit": 90, "rating": 4.6, "tags": ["现代", "夜景", "江景"]},
        ],
    },
    "南京": {
        "center": (118.797, 32.060),
        "pois": [
            {"name": "中山陵", "category": "历史", "address": "玄武区石象路7号", "lon": 118.854, "lat": 32.065, "visit": 120, "rating": 4.7, "tags": ["历史", "古迹", "自然"]},
            {"name": "总统府", "category": "历史", "address": "玄武区长江路292号", "lon": 118.796, "lat": 32.059, "visit": 120, "rating": 4.6, "tags": ["历史", "古迹"]},
            {"name": "夫子庙", "category": "美食", "address": "秦淮区夫子庙", "lon": 118.790, "lat": 32.041, "visit": 90, "rating": 4.4, "tags": ["美食", "小吃", "历史"]},
            {"name": "侵华日军南京大屠杀遇难同胞纪念馆", "category": "历史", "address": "建邺区水西门大街418号", "lon": 118.735, "lat": 32.040, "visit": 120, "rating": 4.7, "tags": ["历史", "博物馆"]},
            {"name": "玄武湖", "category": "自然", "address": "玄武区玄武巷1号", "lon": 118.796, "lat": 32.070, "visit": 120, "rating": 4.6, "tags": ["自然", "公园", "湖景"]},
            {"name": "鸡鸣寺", "category": "宗教", "address": "玄武区鸡鸣寺路1号", "lon": 118.795, "lat": 32.064, "visit": 60, "rating": 4.5, "tags": ["宗教", "历史"]},
            {"name": "南京博物院", "category": "历史", "address": "玄武区中山东路321号", "lon": 118.817, "lat": 32.055, "visit": 150, "rating": 4.8, "tags": ["历史", "博物馆"]},
            {"name": "新街口", "category": "现代", "address": "玄武区新街口", "lon": 118.780, "lat": 32.046, "visit": 90, "rating": 4.3, "tags": ["现代", "购物", "夜景"]},
        ],
    },
    "西安": {
        "center": (108.948, 34.263),
        "pois": [
            {"name": "兵马俑", "category": "历史", "address": "临潼区秦陵北路", "lon": 109.273, "lat": 34.385, "visit": 180, "rating": 4.8, "tags": ["历史", "古迹", "博物馆"]},
            {"name": "大雁塔", "category": "宗教", "address": "雁塔区雁塔路", "lon": 108.964, "lat": 34.219, "visit": 90, "rating": 4.6, "tags": ["宗教", "历史"]},
            {"name": "西安城墙", "category": "历史", "address": "碑林区南大街", "lon": 108.946, "lat": 34.258, "visit": 120, "rating": 4.7, "tags": ["历史", "古迹"]},
            {"name": "回民街", "category": "美食", "address": "莲湖区北院门", "lon": 108.941, "lat": 34.267, "visit": 90, "rating": 4.4, "tags": ["美食", "小吃"]},
            {"name": "陕西历史博物馆", "category": "历史", "address": "雁塔区小寨东路91号", "lon": 108.967, "lat": 34.230, "visit": 150, "rating": 4.8, "tags": ["历史", "博物馆"]},
            {"name": "大唐不夜城", "category": "夜景", "address": "雁塔区慈恩路", "lon": 108.966, "lat": 34.217, "visit": 90, "rating": 4.5, "tags": ["夜景", "现代", "美食"]},
            {"name": "华清宫", "category": "历史", "address": "临潼区华清路", "lon": 109.215, "lat": 34.371, "visit": 120, "rating": 4.5, "tags": ["历史", "古迹", "自然"]},
            {"name": "钟鼓楼", "category": "历史", "address": "碑林区西大街", "lon": 108.940, "lat": 34.261, "visit": 60, "rating": 4.4, "tags": ["历史", "古迹", "夜景"]},
        ],
    },
    "成都": {
        "center": (104.066, 30.657),
        "pois": [
            {"name": "宽窄巷子", "category": "美食", "address": "青羊区同仁路", "lon": 104.056, "lat": 30.670, "visit": 90, "rating": 4.4, "tags": ["美食", "小吃", "历史"]},
            {"name": "大熊猫基地", "category": "亲子", "address": "成华区熊猫大道", "lon": 104.146, "lat": 30.734, "visit": 180, "rating": 4.8, "tags": ["亲子", "自然"]},
            {"name": "武侯祠", "category": "历史", "address": "武侯区武侯祠大街231号", "lon": 104.047, "lat": 30.644, "visit": 90, "rating": 4.6, "tags": ["历史", "古迹", "宗教"]},
            {"name": "锦里", "category": "美食", "address": "武侯区武侯祠大街", "lon": 104.048, "lat": 30.642, "visit": 90, "rating": 4.3, "tags": ["美食", "小吃", "历史"]},
            {"name": "杜甫草堂", "category": "历史", "address": "青羊区青华路37号", "lon": 104.028, "lat": 30.660, "visit": 90, "rating": 4.6, "tags": ["历史", "古迹", "自然"]},
            {"name": "春熙路", "category": "现代", "address": "锦江区春熙路", "lon": 104.081, "lat": 30.656, "visit": 90, "rating": 4.3, "tags": ["现代", "购物", "夜景"]},
            {"name": "都江堰", "category": "历史", "address": "都江堰市公园路", "lon": 103.618, "lat": 31.003, "visit": 150, "rating": 4.7, "tags": ["历史", "古迹", "自然"]},
            {"name": "玉林路小酒馆", "category": "夜景", "address": "武侯区玉林西路", "lon": 104.062, "lat": 30.642, "visit": 60, "rating": 4.2, "tags": ["夜景", "美食"]},
        ],
    },
    "杭州": {
        "center": (120.155, 30.274),
        "pois": [
            {"name": "西湖", "category": "自然", "address": "西湖区龙井路", "lon": 120.149, "lat": 30.259, "visit": 180, "rating": 4.8, "tags": ["自然", "湖景", "公园"]},
            {"name": "灵隐寺", "category": "宗教", "address": "西湖区法云弄1号", "lon": 120.101, "lat": 30.241, "visit": 120, "rating": 4.7, "tags": ["宗教", "历史", "自然"]},
            {"name": "西溪湿地", "category": "自然", "address": "西湖区天目山路", "lon": 120.075, "lat": 30.269, "visit": 150, "rating": 4.6, "tags": ["自然", "公园"]},
            {"name": "河坊街", "category": "美食", "address": "上城区河坊街", "lon": 120.169, "lat": 30.245, "visit": 90, "rating": 4.3, "tags": ["美食", "小吃", "历史"]},
            {"name": "宋城", "category": "现代", "address": "西湖区之江路", "lon": 120.112, "lat": 30.183, "visit": 150, "rating": 4.4, "tags": ["现代", "亲子"]},
            {"name": "雷峰塔", "category": "历史", "address": "西湖区南山路", "lon": 120.149, "lat": 30.231, "visit": 60, "rating": 4.5, "tags": ["历史", "古迹", "夜景"]},
            {"name": "京杭大运河", "category": "夜景", "address": "拱墅区运河边", "lon": 120.158, "lat": 30.318, "visit": 90, "rating": 4.4, "tags": ["夜景", "现代", "自然"]},
            {"name": "浙江省博物馆", "category": "历史", "address": "西湖区孤山路", "lon": 120.147, "lat": 30.255, "visit": 120, "rating": 4.6, "tags": ["历史", "博物馆"]},
        ],
    },
    "广州": {
        "center": (113.264, 23.129),
        "pois": [
            {"name": "广州塔", "category": "现代", "address": "海珠区阅江西路", "lon": 113.324, "lat": 23.106, "visit": 90, "rating": 4.6, "tags": ["现代", "夜景", "江景"]},
            {"name": "陈家祠", "category": "历史", "address": "荔湾区中山七路", "lon": 113.245, "lat": 23.128, "visit": 90, "rating": 4.6, "tags": ["历史", "古迹"]},
            {"name": "沙面", "category": "历史", "address": "荔湾区沙面岛", "lon": 113.243, "lat": 23.106, "visit": 90, "rating": 4.5, "tags": ["历史", "自然", "美食"]},
            {"name": "北京路步行街", "category": "美食", "address": "越秀区北京路", "lon": 113.272, "lat": 23.123, "visit": 90, "rating": 4.3, "tags": ["美食", "小吃", "购物"]},
            {"name": "长隆欢乐世界", "category": "亲子", "address": "番禺区迎宾路", "lon": 113.324, "lat": 22.999, "visit": 240, "rating": 4.6, "tags": ["亲子", "现代"]},
            {"name": "广东省博物馆", "category": "历史", "address": "天河区珠江东路", "lon": 113.323, "lat": 23.118, "visit": 150, "rating": 4.7, "tags": ["历史", "博物馆"]},
            {"name": "上下九", "category": "美食", "address": "荔湾区上下九", "lon": 113.248, "lat": 23.114, "visit": 90, "rating": 4.2, "tags": ["美食", "小吃"]},
            {"name": "白云山", "category": "自然", "address": "白云区同和", "lon": 113.297, "lat": 23.191, "visit": 150, "rating": 4.5, "tags": ["自然", "公园"]},
        ],
    },
    "重庆": {
        "center": (106.551, 29.563),
        "pois": [
            {"name": "洪崖洞", "category": "夜景", "address": "渝中区嘉滨路", "lon": 106.578, "lat": 29.564, "visit": 90, "rating": 4.5, "tags": ["夜景", "美食", "现代"]},
            {"name": "解放碑", "category": "现代", "address": "渝中区民权路", "lon": 106.578, "lat": 29.552, "visit": 60, "rating": 4.3, "tags": ["现代", "购物", "夜景"]},
            {"name": "磁器口古镇", "category": "历史", "address": "沙坪坝区磁器口", "lon": 106.456, "lat": 29.581, "visit": 120, "rating": 4.4, "tags": ["历史", "古迹", "美食"]},
            {"name": "长江索道", "category": "现代", "address": "渝中区新华路", "lon": 106.585, "lat": 29.558, "visit": 60, "rating": 4.4, "tags": ["现代", "江景", "夜景"]},
            {"name": "武隆天生三桥", "category": "自然", "address": "武隆区仙女山镇", "lon": 107.623, "lat": 29.270, "visit": 180, "rating": 4.7, "tags": ["自然", "公园"]},
            {"name": "南山一棵树", "category": "夜景", "address": "南岸区南山", "lon": 106.596, "lat": 29.537, "visit": 60, "rating": 4.4, "tags": ["夜景", "自然"]},
            {"name": "重庆火锅美食街", "category": "美食", "address": "渝中区八一路", "lon": 106.576, "lat": 29.556, "visit": 90, "rating": 4.3, "tags": ["美食", "小吃"]},
            {"name": "湖广会馆", "category": "历史", "address": "渝中区芭蕉园", "lon": 106.590, "lat": 29.557, "visit": 60, "rating": 4.4, "tags": ["历史", "古迹"]},
        ],
    },
}


def sample_pois(city: str) -> list[dict[str, Any]]:
    entry = CITIES.get(city)
    if not entry:
        return []
    return [dict(p, city=city) for p in entry["pois"]]


def known_cities() -> list[str]:
    return list(CITIES.keys())
