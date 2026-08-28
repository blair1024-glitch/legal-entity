# 第三方前端資源

`echarts.min.js` — Apache ECharts 5.5.1，Apache-2.0 授權。

直接放在 repo 裡（而非用 CDN）的理由：儀表板是本機執行的工具，離線、
無外網或內網環境都應該能開得起來，不該因為載不到 CDN 就變成一片空白。

更新方式：

    npm pack echarts@<版本>
    tar -xzf echarts-<版本>.tgz
    cp package/dist/echarts.min.js web/vendor/echarts.min.js
