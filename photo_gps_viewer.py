#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
photo_gps_viewer.py
打开文件夹浏览照片，在高德地图上显示 GPS 位置和拍摄方向（GPSImgDirection）。

依赖安装：
    pip install -r requirements.txt
    # 或：pip install PyQt5 PyQtWebEngine exifread

运行：
    python photo_gps_viewer.py
"""

import sys
import os
import math
import json
import urllib.parse
from pathlib import Path

import exifread
from PySide6.QtCore import Qt, QSize, QUrl, QMimeData, QBuffer, QIODevice
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QLabel, QPushButton,
    QFileDialog, QLineEdit, QStatusBar, QAbstractItemView,
    QDialog, QFormLayout, QDialogButtonBox
)
from PySide6.QtGui import QPixmap, QColor, QImage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage


# ---------------------------------------------------------------------------
# EXIF 解析
# ---------------------------------------------------------------------------
def dms_to_decimal(dms, ref):
    """GPS 度分秒 -> 十进制。"""
    d = float(dms[0].num) / float(dms[0].den)
    m = float(dms[1].num) / float(dms[1].den)
    s = float(dms[2].num) / float(dms[2].den)
    decimal = d + m / 60 + s / 3600
    if str(ref).strip() in ('S', 'W'):
        decimal = -decimal
    return decimal


def read_exif(path):
    """读取 GPS 经纬度、拍摄方向与水平视场角（基于 35mm 等效焦距）。"""
    try:
        with open(path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return None

    out = {
        'lat': None, 'lon': None,
        'bearing': None, 'has_gps': False,
        'h_fov': None,           # 水平视场角（度）
    }

    try:
        lat = tags.get('GPS GPSLatitude')
        lat_ref = tags.get('GPS GPSLatitudeRef')
        lon = tags.get('GPS GPSLongitude')
        lon_ref = tags.get('GPS GPSLongitudeRef')
        if lat and lat_ref and lon and lon_ref:
            out['lat'] = dms_to_decimal(lat.values, lat_ref)
            out['lon'] = dms_to_decimal(lon.values, lon_ref)
            out['has_gps'] = True
    except Exception:
        pass

    try:
        b = tags.get('GPS GPSImgDirection')
        if b:
            v = float(b.values[0].num) / float(b.values[0].den)
            if 0 <= v <= 360:
                out['bearing'] = v
    except Exception:
        pass

    # 水平 FoV：优先用 35mm 等效焦距（不同相机都能用），否则默认 60°
    focal_35 = None
    try:
        for key in ('EXIF FocalLengthIn35mmFilm',):
            t = tags.get(key)
            if t:
                v = float(t.values[0].num) / float(t.values[0].den)
                if v > 0:
                    focal_35 = v
                    break
    except Exception:
        pass
    if focal_35 is None:
        try:
            t = tags.get('EXIF FocalLength')
            if t:
                v = float(t.values[0].num) / float(t.values[0].den)
                if v > 0:
                    # 无 35mm 等效时，按全画幅 36mm 估算
                    focal_35 = v
        except Exception:
            pass

    if focal_35 and focal_35 > 0:
        # 全画幅水平 36mm；对角更准但 FoV 计算通常按水平
        out['h_fov'] = 2.0 * math.degrees(math.atan(36.0 / (2.0 * focal_35)))
        if out['h_fov'] > 60.0:
            out['h_fov'] = 60.0  # 上限
    else:
        out['h_fov'] = 45.0  # 默认

    return out


# ---------------------------------------------------------------------------
# Leaflet + 多地图源 HTML（bootcdn 国内 CDN）
# 天地图 Token 从 ~/.photo_gps_viewer.cfg 读取，
# 也可点击工具栏「设置」按钮修改。
# ---------------------------------------------------------------------------

# 配置 token 持久化
_CFG_PATH = os.path.expanduser('~/.photo_gps_viewer.cfg')

def _load_token():
    if os.path.exists(_CFG_PATH):
        try:
            with open(_CFG_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('tdt_token='):
                        return line.split('=', 1)[1]
        except Exception:
            pass
    return None

def _save_token(token):
    try:
        with open(_CFG_PATH, 'w', encoding='utf-8') as f:
            f.write('tdt_token=' + token + '\n')
    except Exception:
        pass

# 源码中的默认 token，仅在用户从未通过设置界面保存过时使用。
# 发布给其他人时这一行留空，让用户走设置界面自行申请。
_DEFAULT_TDT_TOKEN = ''

_saved = _load_token()
if _saved is not None:
    TDT_TOKEN = _saved
elif _DEFAULT_TDT_TOKEN:
    TDT_TOKEN = _DEFAULT_TDT_TOKEN
    _save_token(TDT_TOKEN)
else:
    TDT_TOKEN = ''


def build_map_html(token):
    """根据 token 生成地图 HTML 字符串。"""
    html = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>map</title>
<link rel="stylesheet" href="https://cdn.bootcdn.net/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
  html, body, #map { margin:0; padding:0; height:100%; width:100%; background:#e8e8e8; }
  .leaflet-control-attribution { font-size: 10px; }
  #status { position:absolute; left:8px; bottom:8px; z-index:1000;
            background:rgba(0,0,0,0.7); color:#fff; padding:4px 8px;
            font:12px/1.4 monospace; border-radius:3px; display:none; }
  .pin-icon { background:#2c3e50; color:#fff; border:3px solid #f1c40f;
              border-radius:50%; width:30px; height:30px;
              display:flex; align-items:center; justify-content:center;
              font:bold 13px sans-serif; box-shadow:0 2px 6px rgba(0,0,0,0.6); }
</style>
</head>
<body>
<div id="map"></div>
<div id="status"></div>
<script>
  function log(msg) {
    var s = document.getElementById('status');
    s.style.display = 'block';
    s.textContent = msg;
  }
  window.onerror = function (msg, url, line, col, err) {
    log('JS错误: ' + msg + ' (line ' + line + ')');
  };
</script>
<script src="https://cdn.bootcdn.net/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
  // WGS84 → GCJ02（火星坐标）转换，仅高德图层需要
  var coordTransform = {
    outOfChina: function (lng, lat) {
      return lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271;
    },
    transformLat: function (x, y) {
      var r = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*Math.sqrt(Math.abs(x));
      r += (20.0*Math.sin(6.0*x*Math.PI) + 20.0*Math.sin(2.0*x*Math.PI)) * 2.0/3.0;
      r += (20.0*Math.sin(y*Math.PI) + 40.0*Math.sin(y/3.0*Math.PI)) * 2.0/3.0;
      r += (160.0*Math.sin(y/12.0*Math.PI) + 320.0*Math.sin(y*Math.PI/30.0)) * 2.0/3.0;
      return r;
    },
    transformLng: function (x, y) {
      var r = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*Math.sqrt(Math.abs(x));
      r += (20.0*Math.sin(6.0*x*Math.PI) + 20.0*Math.sin(2.0*x*Math.PI)) * 2.0/3.0;
      r += (20.0*Math.sin(x*Math.PI) + 40.0*Math.sin(x/3.0*Math.PI)) * 2.0/3.0;
      r += (150.0*Math.sin(x/12.0*Math.PI) + 300.0*Math.sin(x/30.0*Math.PI)) * 2.0/3.0;
      return r;
    },
    wgs84ToGcj02: function (lng, lat) {
      if (this.outOfChina(lng, lat)) return [lng, lat];
      var dLat = this.transformLat(lng - 105.0, lat - 35.0);
      var dLng = this.transformLng(lng - 105.0, lat - 35.0);
      var radLat = lat / 180.0 * Math.PI;
      var magic = Math.sin(radLat);
      magic = 1 - 0.00669342162296594323 * magic * magic;
      var sqrtMagic = Math.sqrt(magic);
      dLat = (dLat * 180.0) / ((6378245.0 * (1 - 0.00669342162296594323)) / (magic * sqrtMagic) * Math.PI);
      dLng = (dLng * 180.0) / (6378245.0 / sqrtMagic * Math.cos(radLat) * Math.PI);
      return [lng + dLng, lat + dLat];
    }
  };
</script>
<script>
  try {
    var TDT_TOKEN = "__TDT_TOKEN__";
    var map = L.map('map', { zoomControl: true, maxZoom: 20 }).setView([35, 105], 4);

    var baseLayers = {};

    var gaodeVec = L.tileLayer(
      'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
      { subdomains: ['1','2','3','4'], maxZoom: 20, maxNativeZoom: 18, attribution: '高德' }
    );
    baseLayers['高德地图'] = gaodeVec;

    var gaodeSat = L.layerGroup([
      L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
        { subdomains: ['1','2','3','4'], maxZoom: 20, maxNativeZoom: 18 }),
      L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}',
        { subdomains: ['1','2','3','4'], maxZoom: 20, maxNativeZoom: 18 })
    ]);
    baseLayers['高德卫星'] = gaodeSat;

    if (TDT_TOKEN) {
      var tdtVec = L.tileLayer(
        'https://t{s}.tianditu.gov.cn/vec_w/wmts?layer=vec&style=default&tilematrixset=w'
        + '&Service=WMTS&Request=GetTile&Version=1.0.0&Format=tiles'
        + '&TileMatrix={z}&TileCol={x}&TileRow={y}&tk=' + TDT_TOKEN,
        { subdomains: ['0','1','2','3','4','5','6','7'], maxZoom: 20, maxNativeZoom: 18, attribution: '天地图' }
      );
      baseLayers['天地图'] = tdtVec;

      var tdtSat = L.layerGroup([
        L.tileLayer('https://t{s}.tianditu.gov.cn/img_w/wmts?layer=img&style=default&tilematrixset=w'
          + '&Service=WMTS&Request=GetTile&Version=1.0.0&Format=tiles'
          + '&TileMatrix={z}&TileCol={x}&TileRow={y}&tk=' + TDT_TOKEN,
          { subdomains: ['0','1','2','3','4','5','6','7'], maxZoom: 20, maxNativeZoom: 18 }),
        L.tileLayer('https://t{s}.tianditu.gov.cn/cia_w/wmts?layer=cia&style=default&tilematrixset=w'
          + '&Service=WMTS&Request=GetTile&Version=1.0.0&Format=tiles'
          + '&TileMatrix={z}&TileCol={x}&TileRow={y}&tk=' + TDT_TOKEN,
          { subdomains: ['0','1','2','3','4','5','6','7'], maxZoom: 20, maxNativeZoom: 18 })
      ]);
      baseLayers['天地图影像'] = tdtSat;
    }

    gaodeVec.addTo(map);
    L.control.layers(baseLayers, {}, { collapsed: false }).addTo(map);

    var activeBase = gaodeVec;
    map.on('baselayerchange', function (e) { activeBase = e.layer; });

    // 高德用 GCJ02，天地图 _w 用 WGS84；按当前底图自动转换
    function toDisplay(lat, lon) {
      if (activeBase === gaodeVec || activeBase === gaodeSat) {
        var t = coordTransform.wgs84ToGcj02(lon, lat);
        return [t[1], t[0]];
      }
      return [lat, lon];
    }

    var mainGroup = L.layerGroup().addTo(map);
    var pinGroup  = L.layerGroup().addTo(map);

    var currentPhotos = [];
    var currentPins   = [];

    function destPoint(lat, lon, brg, dist) {
      var R = 6378137;
      var br = brg * Math.PI / 180;
      var lat1 = lat * Math.PI / 180;
      var lon1 = lon * Math.PI / 180;
      var dR = dist / R;
      var lat2 = Math.asin(Math.sin(lat1)*Math.cos(dR) +
                           Math.cos(lat1)*Math.sin(dR)*Math.cos(br));
      var lon2 = lon1 + Math.atan2(Math.sin(br)*Math.sin(dR)*Math.cos(lat1),
                                    Math.cos(dR) - Math.sin(lat1)*Math.sin(lat2));
      return [lat2 * 180 / Math.PI, lon2 * 180 / Math.PI];
    }

    // 三角形距离随当前缩放级别动态调整：z=18 时 125m，每放大一级减半，每缩小一级翻倍
    function getTriangleDistance() {
      var z = map.getZoom();
      return Math.max(15, Math.min(3000, 125 * Math.pow(2, 18 - z)));
    }

    var COLOR = '#e74c3c';

    function makeNumberedIcon(n) {
      var html = '<div style="background:' + COLOR + ';color:#fff;border:2px solid #fff;'
        + 'border-radius:50%;width:28px;height:28px;display:flex;'
        + 'align-items:center;justify-content:center;font:bold 13px sans-serif;'
        + 'box-shadow:0 1px 4px rgba(0,0,0,0.5);">' + n + '</div>';
      return L.divIcon({ className: '', html: html, iconSize: [28, 28], iconAnchor: [14, 14] });
    }

    function makePinIcon() {
      return L.divIcon({
        className: '',
        html: '<div class="pin-icon">📌</div>',
        iconSize: [30, 30], iconAnchor: [15, 15]
      });
    }

    function makeFovTriangle(lat, lon, bearing, h_fov, distance_m) {
      var half = (h_fov || 45) / 2;
      var steps = 6;
      var arcPts = [];
      for (var i = 1; i <= steps; i++) {
        var b = bearing - half + (2 * half) * i / steps;
        arcPts.push(destPoint(lat, lon, b, distance_m));
      }
      var ring = [[lat, lon]].concat(arcPts);
      return L.polygon(ring, {
        color: COLOR, weight: 2, opacity: 0.9,
        fillColor: COLOR, fillOpacity: 0.2,
        interactive: false
      });
    }

    function renderPhotos(photos, targetGroup, pinMode) {
      var bounds = [];
      photos.forEach(function (p, i) {
        var ll = toDisplay(p.lat, p.lon);
        bounds.push(ll);
        var hasDirection = p.bearing != null && !isNaN(p.bearing);

        var marker = L.marker(ll, {
          icon: pinMode ? makePinIcon() : makeNumberedIcon(i + 1),
          title: p.name || ''
        });

        if (p.path) {
          marker.on('click', function () {
            var f = document.createElement('iframe');
            f.style.display = 'none';
            f.src = 'py://show?path=' + encodeURIComponent(p.path);
            document.body.appendChild(f);
            setTimeout(function () { document.body.removeChild(f); }, 50);
          });
        }

        marker.addTo(targetGroup);

        if (hasDirection) {
          var dist = getTriangleDistance();
          // ll is already converted to the active map coordinate system.
          makeFovTriangle(ll[0], ll[1], p.bearing, p.h_fov || 45, dist)
            .addTo(targetGroup);
        }
      });
      return bounds;
    }

    window.showPhotos = function (photos) {
      currentPhotos = photos || [];
      mainGroup.clearLayers();
      if (currentPhotos.length === 0) { log(''); return; }
      log('显示 ' + currentPhotos.length + ' 张');
      var bounds = renderPhotos(currentPhotos, mainGroup, false);
      if (currentPhotos.length === 1) {
        map.setView(toDisplay(currentPhotos[0].lat, currentPhotos[0].lon),
                    Math.max(map.getZoom(), 17));
      } else {
        map.fitBounds(bounds, { padding: [50, 50], maxZoom: 18 });
      }
    };

    window.showPins = function (photos) {
      currentPins = photos || [];
      pinGroup.clearLayers();
      if (currentPins.length === 0) { log('已清除标注'); return; }
      var bounds = renderPhotos(currentPins, pinGroup, true);
      log('已标注 ' + currentPins.length + ' 张');
      map.fitBounds(bounds, { padding: [50, 50], maxZoom: 18 });
    };

    window.clearPins = function () {
      currentPins = [];
      pinGroup.clearLayers();
      log('标注已清除');
    };

    window.clearAll = function () {
      currentPhotos = [];
      currentPins = [];
      mainGroup.clearLayers();
      pinGroup.clearLayers();
      log('');
    };

    // 缩放结束后重绘两组三角形，使其大小随缩放变化
    map.on('zoomend', function () {
      if (currentPhotos.length > 0) {
        mainGroup.clearLayers();
        renderPhotos(currentPhotos, mainGroup, false);
      }
      if (currentPins.length > 0) {
        pinGroup.clearLayers();
        renderPhotos(currentPins, pinGroup, true);
      }
    });

    log('地图就绪');
  } catch (e) {
    log('初始化失败: ' + e.message);
  }
</script>
</body>
</html>"""
    return html.replace(
        '"__TDT_TOKEN__"',
        ('"' + token + '"') if token else '""'
    )


MAP_HTML = build_map_html(TDT_TOKEN)


# ---------------------------------------------------------------------------
# 设置对话框：编辑天地图 Token
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, current_token, parent=None):
        super().__init__(parent)
        self.setWindowTitle('设置')
        self.resize(520, 180)

        layout = QFormLayout(self)

        self.token_edit = QLineEdit(current_token)
        self.token_edit.setPlaceholderText('留空则不显示天地图和天地图影像')
        layout.addRow('天地图 Token:', self.token_edit)

        info = QLabel(
            '免费申请地址：\n'
            '  https://console.tianditu.gov.cn/api/key\n\n'
            '保存后会写入 ~/.photo_gps_viewer.cfg 并立即重载地图。'
        )
        info.setStyleSheet('color:#666; font-size:11px;')
        info.setWordWrap(True)
        layout.addRow(info)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_token(self):
        return self.token_edit.text().strip()


# ---------------------------------------------------------------------------
# 自定义 WebEnginePage：拦截 py:// 协议实现 JS→Python 通信
# ---------------------------------------------------------------------------
class MapPage(QWebEnginePage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._handler = None

    def set_handler(self, handler):
        self._handler = handler

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        s = url.toString()
        if s.startswith('py://'):
            if self._handler:
                idx = s.find('?path=')
                if idx >= 0:
                    path = urllib.parse.unquote(s[idx + 6:])
                    self._handler(path)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('照片位置查看器')
        self.resize(1320, 820)

        self._current_pixmap = None
        self._map_ready = False
        self._pending_js = None

        top = QWidget()
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(8, 8, 8, 4)
        top_lay.addWidget(QLabel('文件夹：'))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText('点击右侧按钮选择包含照片的文件夹…')
        self.btn_browse = QPushButton('浏览文件夹…')
        top_lay.addWidget(self.path_edit, 1)
        top_lay.addWidget(self.btn_browse)
        top_lay.addSpacing(12)
        self.btn_mark = QPushButton('📌 标注选中')
        self.btn_clear_pins = QPushButton('✕ 清除标注')
        self.btn_copy = QPushButton('复制选中图片')
        self.btn_copy.setShortcut('Ctrl+C')
        top_lay.addWidget(self.btn_mark)
        top_lay.addWidget(self.btn_clear_pins)
        top_lay.addWidget(self.btn_copy)
        top_lay.addSpacing(12)
        self.btn_settings = QPushButton('⚙ 设置')
        top_lay.addWidget(self.btn_settings)

        self.list_widget = QListWidget()
        self.list_widget.setMinimumWidth(240)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)

        self.image_label = QLabel('选择一张图片')
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(
            'background:#1a1a1a;color:#aaa;font-size:14px;'
        )
        self.image_label.setMinimumHeight(240)

        self.web = QWebEngineView()
        page = MapPage(self.web)
        page.set_handler(self._on_marker_clicked)
        self.web.setPage(page)
        self.web.setHtml(MAP_HTML)

        right_split = QSplitter(Qt.Vertical)
        right_split.addWidget(self.image_label)
        right_split.addWidget(self.web)
        right_split.setSizes([380, 440])

        main_split = QSplitter(Qt.Horizontal)
        main_split.addWidget(self.list_widget)
        main_split.addWidget(right_split)
        main_split.setSizes([260, 1060])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(top)
        layout.addWidget(main_split, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())

        self.btn_browse.clicked.connect(self.choose_folder)
        self.path_edit.returnPressed.connect(self.load_folder)
        self.btn_mark.clicked.connect(self._on_mark_clicked)
        self.btn_clear_pins.clicked.connect(self._on_clear_pins_clicked)
        self.btn_copy.clicked.connect(self._on_copy_clicked)
        self.btn_settings.clicked.connect(self._on_settings_clicked)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.web.loadFinished.connect(self._on_map_loaded)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '选择文件夹')
        if not folder:
            return
        self.path_edit.setText(folder)
        self.load_folder()

    def load_folder(self):
        folder = self.path_edit.text().strip().strip('"').strip("'")
        if not folder or not os.path.isdir(folder):
            self.statusBar().showMessage('路径无效', 3000)
            return

        self.list_widget.clear()
        self.image_label.setText('选择一张图片')
        self.image_label.setPixmap(QPixmap())
        self._current_pixmap = None
        self._run_js('clearAll();')

        exts = {'.jpg', '.jpeg', '.tif', '.tiff', '.heic', '.heif'}
        items = []
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if Path(name).suffix.lower() in exts:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, folder).replace(os.sep, '/')
                    items.append((rel, full))
        items.sort(key=lambda x: x[0].lower())

        gps_count = 0
        for label, full in items:
            exif = read_exif(full)
            has = bool(exif and exif['has_gps'])
            has_direction = bool(exif and exif['bearing'] is not None)
            if has:
                gps_count += 1
            text = ('✓ ' if has else '  ') + label
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, full)
            item.setData(Qt.UserRole + 1, exif)  # 缓存 EXIF，避免每次点击重读
            if has_direction:
                item.setForeground(QColor('#e67e22'))
            elif has:
                item.setForeground(QColor('#1a8a3a'))
            self.list_widget.addItem(item)

        self.statusBar().showMessage(f'共 {len(items)} 张图片，其中 {gps_count} 张含 GPS')

    def _on_selection_changed(self):
        """统一处理选择变化：preview 跟随 current，map 跟随所有选中项。"""
        selected = self.list_widget.selectedItems()
        current = self.list_widget.currentItem()
        if current is not None:
            self._show_preview(current.data(Qt.UserRole))
        self._show_on_map(selected)

    def _on_marker_clicked(self, path):
        """地图标记被点击 → 在列表中定位并选中对应项。"""
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == path:
                self.list_widget.setCurrentItem(item)
                self.list_widget.scrollToItem(item)
                break

    def _on_mark_clicked(self):
        """标注当前选中：替换之前的标注。"""
        selected = self.list_widget.selectedItems()
        if not selected:
            self.statusBar().showMessage('请先选中要标注的图片', 3000)
            return
        self._show_pins(selected)

    def _on_clear_pins_clicked(self):
        """清除所有标注。"""
        self._run_js('clearPins();')
        self.statusBar().showMessage('已清除标注', 2000)

    def _on_copy_clicked(self):
        """复制文件，并为单张照片提供 InDesign 可粘贴的图片数据。"""
        paths = [
            item.data(Qt.UserRole)
            for item in self.list_widget.selectedItems()
            if item.data(Qt.UserRole) and os.path.isfile(item.data(Qt.UserRole))
        ]
        if not paths:
            self.statusBar().showMessage('请先选中要复制的图片', 3000)
            return

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])

        # InDesign 不接收 Windows 文件拖放格式，但可接收标准图片数据。
        # 单张时额外放入 PNG 位图；PowerPoint/资源管理器仍优先使用文件格式。
        if len(paths) == 1:
            image = QImage(paths[0])
            if not image.isNull():
                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                image.save(buffer, 'PNG')
                mime.setImageData(image)
                mime.setData('image/png', buffer.data())

        QApplication.clipboard().setMimeData(mime)
        if len(paths) == 1:
            self.statusBar().showMessage(
                '已复制图片文件和图片数据，可在资源管理器、PowerPoint 或 InDesign 粘贴',
                5000
            )
        else:
            self.statusBar().showMessage(
                f'已复制 {len(paths)} 个图片文件，可在资源管理器或 PowerPoint 粘贴',
                5000
            )

    def _on_settings_clicked(self):
        """打开设置对话框，编辑天地图 Token。"""
        global TDT_TOKEN
        dlg = SettingsDialog(TDT_TOKEN, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            TDT_TOKEN = dlg.get_token()
            _save_token(TDT_TOKEN)
            self._map_ready = False
            self._pending_js = None
            self.web.setHtml(build_map_html(TDT_TOKEN))
            if TDT_TOKEN:
                self.statusBar().showMessage('设置已保存，地图已重载', 3000)
            else:
                self.statusBar().showMessage('已清空 Token，天地图/影像已隐藏', 4000)

    def _show_pins(self, items):
        photos = []
        for it in items:
            path = it.data(Qt.UserRole)
            exif = it.data(Qt.UserRole + 1)
            if not exif or not exif['has_gps']:
                continue
            photos.append({
                'lat': exif['lat'],
                'lon': exif['lon'],
                'bearing': exif['bearing'],
                'h_fov': exif['h_fov'],
                'dist': 250,
                'name': Path(path).name,
                'path': path,
            })
        if not photos:
            self.statusBar().showMessage('选中的图片无 GPS，无法标注', 3000)
            return
        self._run_js('showPins(' + json.dumps(photos) + ');')
        self.statusBar().showMessage(f'已标注 {len(photos)} 张（再次点击会替换）')

    def _show_preview(self, path):
        if not path or not os.path.exists(path):
            return
        pix = QPixmap(path)
        if not pix.isNull():
            self._current_pixmap = pix
            self._refresh_image()
        else:
            self._current_pixmap = None
            self.image_label.setText('无法加载图片（HEIC 可能需要额外解码库）')

    def _show_on_map(self, items):
        photos = []
        no_gps = 0
        for it in items:
            path = it.data(Qt.UserRole)
            if not path:
                continue
            exif = it.data(Qt.UserRole + 1)  # 使用缓存，不再读盘
            if not exif or not exif['has_gps']:
                no_gps += 1
                continue
            photos.append({
                'lat': exif['lat'],
                'lon': exif['lon'],
                'bearing': exif['bearing'],
                'h_fov': exif['h_fov'],
                'dist': 250,
                'name': Path(path).name,
                'path': path,
            })

        if not photos:
            self._run_js('clearAll();')
            if items:
                self.statusBar().showMessage(
                    f'选中 {len(items)} 张，均无 GPS 信息', 4000)
            return

        self._run_js('showPhotos(' + json.dumps(photos) + ');')

        if no_gps:
            self.statusBar().showMessage(
                f'地图显示 {len(photos)} 张（{no_gps} 张无 GPS 已忽略）')
        else:
            self.statusBar().showMessage(f'地图显示 {len(photos)} 张')

    def _refresh_image(self):
        if self._current_pixmap is None:
            return
        target = self.image_label.size() - QSize(20, 20)
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._current_pixmap.scaled(
            target, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_image()

    def _run_js(self, code):
        if self._map_ready:
            self.web.page().runJavaScript(code)
        else:
            self._pending_js = code

    def _on_map_loaded(self, ok):
        if ok:
            self._map_ready = True
            if self._pending_js:
                self.web.page().runJavaScript(self._pending_js)
                self._pending_js = None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('PhotoGPSViewer')
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
