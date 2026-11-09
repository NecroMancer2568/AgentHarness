import { Activity, Bot, BarChart3, ClipboardList, Clock3, Code2, GitBranch, LayoutDashboard, Play, Settings, ShieldCheck, TerminalSquare } from 'lucide-react'
export type Page = 'overview'|'run'|'benchmark'|'live'|'history'|'metrics'|'repositories'|'models'|'settings'
const items:{id:Page;label:string;icon:any;group:string}[] = [
 {id:'overview',label:'Overview',icon:LayoutDashboard,group:'WORKSPACE'},
 {id:'run',label:'Run evaluation',icon:Play,group:'WORKSPACE'},
 {id:'benchmark',label:'Benchmark suite',icon:ClipboardList,group:'WORKSPACE'},
 {id:'live',label:'Live executions',icon:Activity,group:'WORKSPACE'},
 {id:'history',label:'Run history',icon:Clock3,group:'WORKSPACE'},
 {id:'metrics',label:'Metrics & reports',icon:BarChart3,group:'ANALYTICS'},
 {id:'repositories',label:'Repositories',icon:GitBranch,group:'CONFIGURATION'},
 {id:'models',label:'Ollama & model',icon:Bot,group:'CONFIGURATION'},
 {id:'settings',label:'Settings',icon:Settings,group:'CONFIGURATION'},
]
export default function Sidebar({page,onNavigate}:{page:Page;onNavigate:(p:Page)=>void}){
 return <aside className="sidebar"><div className="brand"><div className="brand-mark"><Code2 size={20}/></div><div><strong>AgentHarness</strong><span>LAB / LOCAL WORKBENCH</span></div></div><div className="side-nav">{['WORKSPACE','ANALYTICS','CONFIGURATION'].map(group=><div key={group} className="nav-group"><div className="nav-label">{group}</div>{items.filter(i=>i.group===group).map(item=>{const Icon=item.icon;return <button key={item.id} className={`nav-item ${page===item.id?'active':''}`} onClick={()=>onNavigate(item.id)}><Icon size={17}/><span>{item.label}</span>{item.id==='live'&&<i className="nav-pulse"/>}</button>})}</div>)}</div><div className="sidebar-bottom"><div className="online-dot"/> Local execution environment <span className="side-version">v1.0</span></div></aside>
}
