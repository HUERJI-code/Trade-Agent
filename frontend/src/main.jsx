// 网页入口：聊天记录由本机 LangGraph 会话存储恢复；浏览器不保存数据库凭据。
import { useEffect, useRef, useState } from 'react'; // 状态、DOM 引用及组件副作用。
import { createRoot } from 'react-dom/client'; // 把 React 挂载到 HTML 的 root 节点。
import './styles.css'; // 构建时输出为本地 app.css。

const examples = ['2019年德国销售额是多少？', '2019年德国按月统计销量', '2019年哪个国家销售额最高？']; // 点击只填入问题，由用户提交。
const dimensions = { order_month: '下单月份', customer_country: '客户国家', product_category: '产品类别' }; // 展示中文列名。
const statuses = { ok: '查询完成', clarify: '需要补充条件', unsupported: '当前不支持', error: '服务暂不可用' }; // 业务状态与 HTTP 状态分开。
const units = { USD: '美元 · 估算', ratio: '% · 估算', units: '件', orders: '单', customers: '位' }; // 金额和毛利率始终保留估算。

function formatValue(value, unit) { // 表格保留数据库的小数字符串精度，百分比只转换用于显示。
  if (value === null || value === undefined) return '—'; // 缺失值绝不显示成零。
  if (unit === 'ratio') return `${(Number(value) * 100).toFixed(2)}%`;
  const [integer, fraction] = String(value).split('.'); // 分离整数和小数再增加千分位。
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return unit === 'USD' ? `${grouped}.${(fraction || '').padEnd(2, '0')}` : grouped;
}

function Chart({ result }) { // 有分组结果时才初始化 Plotly，避免无数据也绘图；依赖已打入本地 bundle。
  const element = useRef(null); // 直接把图表挂到这一 DOM 节点。
  const [failed, setFailed] = useState(false); // 图表异常时仍可阅读下方原始结果表。
  useEffect(() => {
    let disposed = false; // 异步加载完成前组件可能已被新的查询移除。
    let plotly, observer; // 清理时需要访问绘图库和尺寸监听器。
    const node = element.current;
    import('plotly.js-basic-dist-min').then(async module => {
      if (disposed) return;
      plotly = module.default;
      const isMonth = result.dimension === 'order_month';
      const values = result.rows.map(row => row[1] === null ? null : Number(row[1]) * (result.unit === 'ratio' ? 100 : 1)); // 绘图需要浮点数，精确值仍以表格/数据库为准。
      await plotly.newPlot(node, [{
        x: result.rows.map(row => String(row[0])), // 普通分组按维度排序；国家极值查询按完整聚合值排序。
        y: values,
        type: isMonth ? 'scatter' : 'bar', // 时间序列折线，类别或国家柱形图。
        mode: 'lines+markers',
        line: { color: '#197c6f', width: 3 },
        marker: { color: '#197c6f', size: 7 },
        hovertemplate: '%{x}<br>%{y}<extra></extra>', // 不把问题或模型输出拼成 HTML。
      }], {
        height: 340,
        margin: { l: 64, r: 20, t: 22, b: isMonth ? 64 : 105 },
        paper_bgcolor: '#ffffff', plot_bgcolor: '#ffffff',
        font: { family: 'Microsoft YaHei, sans-serif', color: '#61716b', size: 11 },
        xaxis: { type: 'category', gridcolor: '#edf1ee', tickangle: -30, automargin: true },
        yaxis: { title: { text: result.value_label || units[result.unit] }, gridcolor: '#edf1ee', zerolinecolor: '#dbe4de', automargin: true },
        showlegend: false,
      }, { responsive: true, displayModeBar: false }); // 无需云端服务或外部图表 CDN。
      if (disposed) { plotly.purge(node); return; }
      observer = new ResizeObserver(() => plotly.Plots.resize(node)); // 页面宽度变化后重绘尺寸。
      observer.observe(node);
    }).catch(() => { if (!disposed) setFailed(true); }); // 图表失败不会覆盖成功的查询结果。
    return () => { disposed = true; observer?.disconnect(); if (plotly) plotly.purge(node); }; // 卸载时释放图表监听器。
  }, [result]); // 每份新结果只建立一次图表。
  return failed ? <p role="alert">图表加载失败，请查看下方数据表。</p> : <div className="chart" ref={element} role="img" aria-label={`${result.label}分组图表`} />;
}

function AnswerText({ text }) { // 只识别训练风格中的 **加粗**，不把模型文本作为 HTML 执行。
  return String(text).split(/(\*\*[^*]+\*\*)/g).map((part, index) => part.startsWith('**') ? <strong key={index}>{part.slice(2, -2)}</strong> : part);
}

function Result({ response }) { // 展示当前结果，历史消息由会话区域显示。
  const result = response.result;
  const plan = response.plan;
  const genericChart = result?.kind === 'sql' && result.columns.length === 2 && result.rows.length > 1
    && result.rows.length <= 100 && result.rows.every(row => typeof row[0] === 'string' && Number.isNaN(Number(row[0]))
      && (row[1] === null || (String(row[1]).trim() !== '' && Number.isFinite(Number(row[1])))));
  // 自由 SELECT 可以返回任意数量和类型的列，不能按旧“维度、指标、明细数”位置解释。
  if (result?.kind === 'sql') return <section className="result-card" aria-label="分析结果">
    <div className="section-top"><span className={`badge ${response.status}`}>{statuses[response.status]}</span><small>{(response.elapsed_ms / 1000).toFixed(2)} 秒</small></div>
    <h2>查询结果</h2>
    <p className="answer"><AnswerText text={response.answer} /></p>
    {genericChart && <Chart result={{ ...result, dimension: /月|month/i.test(result.columns[0]) ? 'order_month' : 'sql_group', unit: 'raw', value_label: result.columns[1] }} />}
    {result.rows.length > 0 && <div className="table-scroll"><table>
      <caption>数据库返回明细 · {result.rows.length} 行</caption>
      <thead><tr>{result.columns.map((column, index) => <th key={index}>{column}</th>)}</tr></thead>
      <tbody>{result.rows.map((row, index) => <tr key={index}>{row.map((value, cell) => <td key={cell}>{value === null ? '—' : String(value)}</td>)}</tr>)}</tbody>
    </table></div>}
    {result.data_coverage && <p className="footnote">样例销售数据覆盖：{result.data_coverage.first_date} 至 {result.data_coverage.last_date}。</p>}
    {result.limit_applied && result.limit_reached && <p className="footnote">已触及系统返回上限，结果可能不完整。</p>}
    {result.limitations?.map(text => <p className="footnote" key={text}>{text}</p>)}
    <details><summary>查看执行 SQL</summary><pre>{result.sql}</pre></details>
    <p className="trace">查询追踪号：{response.trace_id}</p>
  </section>;
  return <section className="result-card" aria-label="分析结果">
    <div className="section-top"><span className={`badge ${response.status}`}>{statuses[response.status]}</span><small>{(response.elapsed_ms / 1000).toFixed(2)} 秒</small></div>
    <h2>{result?.label || statuses[response.status]}</h2>
    <p className="answer"><AnswerText text={response.answer} /></p>
    {response.inherited_conditions?.length > 0 && <p className="footnote">已结合本对话的上一轮条件：{response.effective_question}</p>}
    {plan?.action === 'query' && <div className="conditions" aria-label="解析条件">
      <span>客户国家：{plan.country || '全部'}</span><span>{plan.start} → {plan.end}（不含）</span><span>{dimensions[plan.dimension] || '不分组'}</span>{result?.ranking && <span>{result.ranking.sort_order === 'desc' ? '比较最高值 · 降序' : '比较最低值 · 升序'}</span>}
    </div>}
    {result && <>
      {result.rows.length > 0 && !result.dimension && <div className="metric-value">{formatValue(result.rows[0][0], result.unit)}<small>{result.unit === 'ratio' ? '估算毛利率' : units[result.unit]}</small></div>}
      {result.rows.length > 0 && result.dimension && <Chart result={result} />}
      {result.rows.length > 0 && <div className="table-scroll"><table>
        <caption>数据库返回明细 · {result.rows.length} 行</caption>
        <thead><tr>{result.dimension && <th>{dimensions[result.dimension]}</th>}<th>{result.label}（{units[result.unit]}）</th><th>匹配销售明细数</th></tr></thead>
        <tbody>{result.rows.map((row, index) => <tr key={index}>
          {result.dimension && <td>{row[0] ?? '—'}</td>}
          <td title={String(row[result.dimension ? 1 : 0] ?? '')}>{formatValue(row[result.dimension ? 1 : 0], result.unit)}</td>
          <td>{row[result.dimension ? 2 : 1]}</td>
        </tr>)}</tbody>
      </table></div>}
      <p className="footnote">数据覆盖：{result.data_coverage.first_date} 至 {result.data_coverage.last_date}。匹配明细数不是去重订单数；分组后的去重计数不可直接相加。</p>
      {result.ranking && <p className="footnote">按完整国家汇总值精确比较，并列国家全部列出；表格保留全部分组，空值不参与比较。此类明确句式由规则识别，不调用模型。</p>}
      {result.limitations?.map(text => <p className="footnote" key={text}>{text}</p>)}
      <details><summary>查看执行 SQL 与解析计划</summary><pre>{result.sql}</pre><pre>{JSON.stringify(plan, null, 2)}</pre></details>
    </>}
    <p className="trace">查询追踪号：{response.trace_id}</p>
  </section>;
}

function App() { // 主页面负责输入、HTTP 请求和当前结果。
  const [question, setQuestion] = useState('');
  const [response, setResponse] = useState(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [health, setHealth] = useState(null);
  const [checking, setChecking] = useState(false);
  const [conversations, setConversations] = useState([]); // 左侧会话目录由服务端创建。
  const [activeId, setActiveId] = useState(null); // 当前聊天 ID 随每次提问提交。
  const [history, setHistory] = useState([]); // 当前会话的历史消息，切换时整体替换。
  function activateConversation(id) {
    setActiveId(id);
    window.history.replaceState(null, '', `/?conversation=${encodeURIComponent(id)}`); // 刷新后继续当前窗口，URL 仅存会话 ID。
  }
  async function refreshConversations() {
    const reply = await fetch('/api/conversations');
    if (!reply.ok) throw new Error('无法读取会话目录。');
    const items = await reply.json();
    setConversations(items);
    return items;
  }
  async function selectConversation(id) {
    if (pending) return;
    setPending(true); setError('');
    try {
      const reply = await fetch(`/api/conversations/${id}`);
      if (!reply.ok) throw new Error('无法读取会话。');
      const data = await reply.json();
      activateConversation(id); setHistory(data.history); setQuestion('');
      setResponse(data.history.at(-1)?.response || null);
    } catch (failure) { setError(failure.message); }
    finally { setPending(false); }
  }
  async function newConversation() {
    if (pending) return;
    setPending(true); setError('');
    try {
      const reply = await fetch('/api/conversations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!reply.ok) throw new Error('无法新建对话。');
      const data = await reply.json();
      activateConversation(data.id); setHistory([]); setResponse(null); setQuestion('');
      await refreshConversations();
    } catch (failure) { setError(failure.message); }
    finally { setPending(false); }
  }
  async function checkHealth() { // 用户可在启动模型或数据库后手动重查。
    setChecking(true);
    try {
      const reply = await fetch('/api/health', { signal: AbortSignal.timeout(15000) });
      if (!reply.ok) throw new Error('health');
      setHealth(await reply.json());
    } catch { setHealth({ status: 'degraded', model: 'unavailable', database: 'unavailable' }); }
    finally { setChecking(false); }
  }
  useEffect(() => { checkHealth(); }, []); // 页面打开时查一次，不进行后台轮询。
  useEffect(() => { refreshConversations().then(items => {
    const requested = new URLSearchParams(window.location.search).get('conversation');
    const selected = items.find(item => item.id === requested) || items[0];
    if (selected) selectConversation(selected.id);
  }).catch(failure => setError(failure.message)); }, []);
  async function submit(event) {
    event.preventDefault(); // 防止表单让浏览器整页刷新。
    if (pending || !question.trim()) return;
    setPending(true); setError(''); setResponse(null); // 清掉旧结果，防止误认为它属于新问题。
    try {
      const reply = await fetch('/api/ask', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: question.trim(), ...(activeId ? { conversation_id: activeId } : {}) }),
        signal: AbortSignal.timeout(390000), // 容纳两次受限规划调用、一次回答生成及数据库执行。
      });
      const data = await reply.json();
      if (data.status && data.answer) {
        setResponse(data); if (data.conversation_id) activateConversation(data.conversation_id);
        setHistory(items => [...items, { question: question.trim(), response: data }].slice(-30));
        setQuestion('');
        await refreshConversations();
      } // 服务错误也可能提供完整的中文解释。
      else throw new Error(data.detail || '请求失败，请稍后重试。');
    } catch (failure) { setError(failure.name === 'TimeoutError' ? '等待超时，服务可能仍在处理。请稍后再试。' : (failure.message === 'Failed to fetch' ? '无法连接本机分析服务，请检查启动终端。' : failure.message)); }
    finally { setPending(false); }
  }
  return <div className="app-shell">
    <header><a className="brand" href="/" aria-label="Trade Agent 首页"><span className="brand-icon">T</span>TRADE<span className="brand-light">AGENT</span></a><span className="header-label">外贸数据分析工作台</span><span className="local-pill">本机运行</span></header>
    <main>
      <div className="intro"><p className="eyebrow">从业务问题，到可核对的数据</p><h1>用一句话，读懂销售表现。</h1><p>输入时间、市场和指标，查看来自数据库的答案与趋势。</p></div>
      <div className="workspace">
        <div className="main-column">
          {history.length > 0 && <section className="question-card" aria-label="聊天记录"><h2>本轮对话</h2>{history.map((item, index) => <div className="chat-turn" key={index}><p><strong>你：</strong>{item.question}</p><p><strong>助手：</strong><AnswerText text={item.response.answer} /></p></div>)}</section>}
          <section className="question-card"><div className="section-top"><h2>开始分析</h2><span className="step">01 / 提出问题</span></div>
            <form onSubmit={submit}><label htmlFor="question">你想了解什么？</label><textarea id="question" value={question} onChange={event => setQuestion(event.target.value)} maxLength={1000} rows={3} disabled={pending} placeholder="例如：2019年德国按月统计销量" required />
              <div className="form-bottom"><small>{history.length ? '可以继续追问，例如“那法国呢？”' : '可比较多个年份、国家和指标'}</small><button type="submit" disabled={pending || !question.trim()}>{pending ? '正在分析…' : '发送问题 →'}</button></div>
            </form>
            <div className="examples"><span>试试这些问题</span>{examples.map(text => <button type="button" disabled={pending} key={text} onClick={() => setQuestion(text)}>{text}</button>)}</div>
          </section>
          <div aria-live="polite" aria-busy={pending}>
            {error && <div className="notice" role="alert">{error}</div>}
            {pending && <div className="empty-state loading"><span className="spinner" /><h2>正在读取与核对数据</h2><p>首次查询需要加载模型，请稍候。</p></div>}
            {response && <Result response={response} />}
            {!response && !pending && !error && <section className="empty-state"><span className="empty-icon">↗</span><h2>每个答案，都有数据可查</h2><p>查询后，这里将展示指标、解析条件、图表与明细。</p><div className="flow"><span>理解问题</span><i>→</i><span>只读查询</span><i>→</i><span>展示结果</span></div></section>}
          </div>
        </div>
        <aside>
          <section className="side-card"><h2>我的对话</h2><button type="button" className="text-button" disabled={pending} onClick={newConversation}>＋ 新建对话</button><div className="conversation-list">{conversations.map(item => <button type="button" className={item.id === activeId ? 'conversation active' : 'conversation'} key={item.id} disabled={pending} onClick={() => selectConversation(item.id)}>{item.title}</button>)}</div></section>
          <section className="side-card"><p className="eyebrow">运行状态</p><h2>本地分析环境</h2><div className="status-row"><span>语言模型</span><strong>{checking ? '检查中' : health?.model && health.model !== 'unavailable' ? health.model : '未就绪'}</strong></div><div className="status-row"><span>MySQL 数据库</span><strong>{checking ? '检查中' : health?.database === 'ready' ? '只读连接就绪' : '未就绪'}</strong></div><button className="text-button" type="button" onClick={checkHealth} disabled={checking}>重新检查状态 ↻</button></section>
          <section className="side-card"><p className="eyebrow">数据说明</p><h2>全球电子零售样例</h2><p className="side-copy">Kaggle 公开教学数据，非真实企业经营项目。</p><div className="date-range">2016.01.01 — 2021.02.20</div><p className="side-copy">市场按客户所在国家划分，时间按下单日期筛选。</p><div className="estimate-note">销售额、毛利和毛利率按产品标价与单位成本<strong>估算</strong>。</div></section>
          <section className="side-card compact"><h2>当前支持</h2><p className="side-copy">业务指标 · 多国与期间对比<br />条件筛选 · 分组 · 排序与 Top N</p><p className="side-copy">支持多个指标和时间范围，并可分析品牌、品类等可用数据。同一对话可以追问，新对话独立保存。</p></section>
        </aside>
      </div>
    </main>
    <footer>TRADE AGENT <span>教学项目 · 所有金额口径均可核对</span></footer>
  </div>;
}

createRoot(document.getElementById('root')).render(<App />); // 启动页面；普通 JSX 文本由 React 转义。
