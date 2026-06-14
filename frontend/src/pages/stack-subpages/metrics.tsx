
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

export function MetricsView({ stackId }: { stackId: number }) {
  const { data: metrics = [] } = useQuery({
    queryKey: ["stacks", stackId, "metrics"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/metrics` as any);
      return res.data || [];
    },
    refetchInterval: 5000,
  });

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader><CardTitle className="text-base">CPU Usage (%)</CardTitle></CardHeader>
        <CardContent className="h-[300px]">
          {metrics.length > 0 ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={metrics}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="time" />
                <YAxis />
                <Tooltip />
                <Line type="monotone" dataKey="cpu" stroke="#8884d8" />
              </LineChart>
            </ResponsiveContainer>
          ) : <div className="flex h-full items-center justify-center text-muted-foreground">Loading...</div>}
        </CardContent>
      </Card>
      
      <Card>
        <CardHeader><CardTitle className="text-base">Memory Usage (MB)</CardTitle></CardHeader>
        <CardContent className="h-[300px]">
          {metrics.length > 0 ? (
             <ResponsiveContainer width="100%" height="100%">
              <LineChart data={metrics}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="time" />
                <YAxis />
                <Tooltip />
                <Line type="monotone" dataKey="memory" stroke="#82ca9d" />
              </LineChart>
            </ResponsiveContainer>
          ) : <div className="flex h-full items-center justify-center text-muted-foreground">Loading...</div>}
        </CardContent>
      </Card>
    </div>
  );
}
