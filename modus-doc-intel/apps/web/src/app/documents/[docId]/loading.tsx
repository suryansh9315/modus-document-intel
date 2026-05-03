export default function Loading() {
  return (
    <div className="flex items-center justify-center py-20">
      <div className="text-center space-y-4">
        <div className="animate-spin w-10 h-10 border-2 border-brand-500 border-t-transparent rounded-full mx-auto" />
        <p className="text-gray-500 text-sm">Loading document...</p>
      </div>
    </div>
  );
}
