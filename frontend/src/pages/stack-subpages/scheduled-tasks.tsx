import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function ScheduledTasksList() {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base">Scheduled Tasks</CardTitle>
        <Button size="sm">Add Task</Button>
      </CardHeader>
      <CardContent>
        <div className="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground mt-4">
          No scheduled tasks configured for this project.
        </div>
      </CardContent>
    </Card>
  );
}
