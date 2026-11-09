const API = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'
export type Run = {id:string; repository:string; task:string; status:string; created_at:string; updated_at:string; duration_ms:number|null; success:boolean|null; total_tokens:number; prompt_tokens:number; completion_tokens:number; llm_calls:number; error?:string|null; result?:any; metrics?:any; workspace_path?:string; events?:EventItem[]}
export type EventItem = {id:number; run_id:string; timestamp:string; event_type:string; agent?:string|null; message:string; data:Record<string,any>}
export type Repository = {name:string; path:string}
async function request<T>(path:string, init?:RequestInit):Promise<T>{
  const res = await fetch(`${API}${path}`, {headers:{'Content-Type':'application/json', ...(init?.headers||{})}, ...init})
  if(!res.ok){const body=await res.json().catch(()=>({detail:res.statusText})); throw new Error(body.detail||body.error||`HTTP ${res.status}`)}
  return res.json()
}
export const api = {
  health:()=>request<any>('/api/health'), metrics:()=>request<any>('/api/metrics'),
  settings:()=>request<any>('/api/settings'), repositories:()=>request<{repositories:Repository[];root:string}>('/api/repositories'),
  registerRepository:(path:string)=>request<any>('/api/repositories/register',{method:'POST',body:JSON.stringify({path})}),
  cloneRepository:(url:string, name?:string, branch?:string)=>request<any>('/api/repositories/clone',{method:'POST',body:JSON.stringify({url, name: name||undefined, branch: branch||undefined})}),
  uploadZip:async(file:File, name?:string)=>{
    const fd = new FormData()
    fd.append('file', file)
    if(name) fd.append('name', name)
    const res = await fetch(`${API}/api/repositories/upload-zip`, {method:'POST', body:fd})
    if(!res.ok){const body=await res.json().catch(()=>({detail:res.statusText})); throw new Error(body.detail||body.error||`HTTP ${res.status}`)}
    return res.json()
  },
  importFolder:(path:string, name?:string)=>request<any>('/api/repositories/import-folder',{method:'POST',body:JSON.stringify({path, name: name||undefined})}),
  deleteRepository:(name:string)=>request<any>(`/api/repositories/${encodeURIComponent(name)}`,{method:'DELETE'}),
  runs:(limit=50)=>request<{runs:Run[]}>(`/api/runs?limit=${limit}`),
  run:(id:string)=>request<Run>(`/api/runs/${id}`),
  events:(id:string, after=0)=>request<{events:EventItem[]}>(`/api/runs/${id}/events?after_id=${after}`),
  createRun:(body:{repository:string;task:string;test_command:string;max_agent_steps?:number})=>request<{id:string;status:string}>('/api/runs',{method:'POST',body:JSON.stringify(body)}),
  cancel:(id:string)=>request<any>(`/api/runs/${id}/cancel`,{method:'POST'}),
  streamUrl:(id:string)=>`${API}/api/runs/${id}/stream`,
  benchmarkDatasets:()=>request<{datasets:{name:string;task_count:number|null;is_default:boolean;error?:string}[]}>('/api/benchmarks/datasets'),
  benchmarkTasks:(dataset='tasks.jsonl')=>request<{tasks:BenchmarkTask[];dataset:string}>(`/api/benchmarks/tasks?dataset=${encodeURIComponent(dataset)}`),
  benchmarks:()=>request<{benchmarks:BenchmarkRun[]}>('/api/benchmarks'),
  benchmark:(id:string)=>request<BenchmarkRun>(`/api/benchmarks/${id}`),
  startBenchmark:(repetitions:number,dataset='tasks.jsonl')=>request<{id:string;status:string;planned_samples:number}>('/api/benchmarks',{method:'POST',body:JSON.stringify({repetitions,dataset})}),
  cancelBenchmark:(id:string)=>request<any>(`/api/benchmarks/${id}/cancel`,{method:'POST'}),
}

export type BenchmarkTask = {id:string;repository:string;category:string;input:string;test_command:string;max_agent_steps:number;expected_behavior:string}
export type BenchmarkResult = {task_id:string;category:string;repository:string;repetition:number;run_id:string;status:string;success:boolean;duration_ms:number|null;total_tokens:number;prompt_tokens:number;completion_tokens:number;llm_calls:number;test_passed:boolean;review_approved:boolean;error?:string|null}
export type BenchmarkRun = {id:string;status:string;created_at:string;updated_at:string;model:string;dataset:string;task_count:number;repetitions:number;planned_samples:number;completed_samples:number;results?:BenchmarkResult[];summary?:any;error?:string|null}
