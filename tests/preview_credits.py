"""Explicit isolated UI fixture. Run then open http://127.0.0.1:8943/preview.

The production page never loads this bridge. Writes below affect browser memory
only, and the server binds localhost and serves three fixed production assets.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "pages" / "credits"
BRIDGE = r"""
const fixture = {
  values: {points_per_yuan:20,rp_base_points:19,rp_step_points:0.02,global_mode:false,global_daily_yuan:10,global_monthly_yuan:300,group_daily_yuan:2,reserve_yuan:0.212,charged_plugins:[],group_overrides:{'qq:10002000':3},holidays:['2026-10-01'],model_prices:{'deepseek-flash':{offpeak:{input:1,cached:0.02,output:4},peak:{input:2,cached:0.04,output:8}}}},
  revision: 1, balance: 19.88, spent: 0.106,
  groupNames: {'10002000':'太鼓达人交流①群 🥁','10003000':'<img src=x onerror=alert(1)>'}
};
function analyticsFixture(period) {
  const length = period === 'month' ? 693 : period === '7d' ? 165 : 21;
  const end = Date.parse('2026-10-29T20:30:00+08:00');
  const first = end - (length - 0.5) * 3600000;
  const stamp = (time) => new Date(time + 8 * 3600000).toISOString().slice(0, 19) + '+08:00';
  const series = Array.from({length}, (_, i) => {
    const hour = i % 24;
    const rate = hour < 7 ? 0 : hour < 12 ? 0.024 * (hour - 6) : hour < 18 ? 0.19 + 0.012 * (hour - 12) : 0.07;
    return {start:stamp(first+i*3600000),cost_yuan:rate*(i===length-1?0.5:1),rate_yuan_per_hour:rate,requests:hour<7?0:12,total_tokens:hour<7?0:100000,partial:i===length-1};
  });
  const groups = ['10002000','10003000',''].map((group_id,i)=>({platform:'qq',group_id,group_name:fixture.groupNames[group_id]||'',cost_yuan:0.3/(i+1),requests:20-i*5,share:0.5/(i+1)}));
  return {period,start:stamp(first),end:stamp(end),series,cost_yuan:0.6,requests:40,total_tokens:300000,recent_hour_yuan:0.08,pending_count:1,reserved_yuan:0.212,peak_hours:[17,16,15],quiet_hours:[0,1,2],hour_profile:Array.from({length:24},(_,hour)=>({hour,average_yuan_per_hour:hour<7?0:0.1})),groups,users:[{platform:'qq',user_id:'10001',name:'测试用户',cost_yuan:0.4,requests:20,share:0.67},{platform:'qq',user_id:'10002',name:'<img src=x onerror=alert(1)>',cost_yuan:0.2,requests:10,share:0.33}]};
}
window.AstrBotPluginPage = {
  ready: async () => ({isDark:false}),
  apiGet: async (path, query={}) => {
    if(path==='credits/settings') return {revision:fixture.revision,values:structuredClone(fixture.values)};
    if(path==='credits/plugins') return [{name:'astrbot_plugin_liuyao',display_name:'六爻',version:'0.25.0',activated:true,charged:false},{name:'astrbot_plugin_wuxing_num',display_name:'五行数字',version:'1.7.0',activated:true,charged:false},{name:'astrbot_plugin_test',display_name:'<img src=x onerror=alert(1)>',activated:false,charged:false}];
    if(path==='credits/quote') return {fixture:true,points_per_yuan:fixture.values.points_per_yuan};
    if(path==='credits/overview') return {day:'2026-10-29',global_mode:fixture.values.global_mode,monthly:{limit_yuan:fixture.values.global_monthly_yuan,spent_yuan:8.3,reserved_yuan:0.212,remaining_yuan:fixture.values.global_monthly_yuan-8.512},global:{limit_yuan:10,spent_yuan:fixture.spent,reserved_yuan:0.212,remaining_yuan:9.682},groups:['10002000','10003000','10004000'].map((group_id)=>({platform:'qq',group_id,group_name:fixture.groupNames[group_id]||'',limit_yuan:3,spent_yuan:fixture.spent,reserved_yuan:0.212,remaining_yuan:2.682,is_override:true})),totals:{spent_yuan:58.2,users:2,points:fixture.balance+20.54},pending_count:1,integration:{fixture:true,rp_connected:true,providers_wrapped:1}};
    if(path==='credits/analytics') return analyticsFixture(query.period || 'today');
    if(path==='credits/users') return {page:Number(query.page||1),page_size:30,total:2,items:[{platform:'qq',user_id:'10001',name:'测试用户',balance:fixture.balance,amount_yuan:fixture.balance/fixture.values.points_per_yuan},{platform:'qq',user_id:'10002',name:'<script>不可执行的昵称</script>',balance:20.54,amount_yuan:1.027}]};
    if(path==='credits/ledger') return {page:Number(query.page||1),page_size:30,total:3,items:[{id:3,occurred_at:'2026-10-29T15:30:00+08:00',platform:'qq',user_id:'10001',group_id:'10002000',group_name:fixture.groupNames['10002000'],kind:'consumption',source_plugin:'chat',points_delta:null,cost_yuan:null,total_tokens:null,status:'unknown',request_id:'fixture-unknown-request',details:{price:{model:'deepseek-flash',tier:'peak'},usage:null}},{id:2,occurred_at:'2026-10-29T08:30:00+08:00',platform:'qq',user_id:'10001',group_id:'10003000',group_name:fixture.groupNames['10003000'],kind:'consumption',source_plugin:'chat',points_delta:-2.12,cost_yuan:0.106,total_tokens:100000,status:'settled',request_id:'fixture-request-2',details:{price:{input:1,cached:0.02,output:4,tier:'offpeak',points_per_yuan:20},usage:{cache_miss_tokens:98000,cache_hit_tokens:0,output_tokens:2000}}},{id:1,occurred_at:'2026-10-29T07:00:00+08:00',platform:'qq',user_id:'10001',group_id:'',kind:'rp',points_delta:20,cost_yuan:0,total_tokens:0,status:'settled',details:{rp:50}}]};
    throw Error('fixture endpoint missing: '+path);
  },
  apiPost: async (path,payload) => {
    if(path==='credits/settings'){if(payload.revision!==fixture.revision)throw Error('配置版本冲突');fixture.values=structuredClone(payload.values);fixture.revision++;return {saved:true};}
    if(path==='credits/adjust'){fixture.balance+=payload.delta;return {saved:true};}
    if(path==='credits/resolve')return {saved:true};
    throw Error('fixture endpoint missing: '+path);
  }
};
"""


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/preview":
            body = (PAGE / "index.html").read_text(encoding="utf-8")
            body = body.replace(
                "<body>",
                '<body><p style="padding:12px;background:#fff2cc;color:#513c00">本地演示数据 · 所有修改仅保存在浏览器内存，不连接真实账本</p>',
            )
            body = body.replace(
                '<script type="module" src="./app.js">',
                '<script src="./fixture-bridge.js"></script><script type="module" src="./app.js">',
            )
            content_type = "text/html; charset=utf-8"
        elif path == "/fixture-bridge.js":
            body, content_type = BRIDGE, "text/javascript; charset=utf-8"
        elif path in {"/app.js", "/style.css"}:
            body = (PAGE / path.lstrip("/")).read_text(encoding="utf-8")
            content_type = (
                "text/javascript; charset=utf-8"
                if path.endswith(".js")
                else "text/css; charset=utf-8"
            )
        else:
            self.send_error(404)
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    print("Credits fixture: http://127.0.0.1:8943/preview", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8943), PreviewHandler).serve_forever()
