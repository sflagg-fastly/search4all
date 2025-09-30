import Image from "next/image";
import { Skeleton } from "@/app/components/skeleton";
import { Wrapper } from "@/app/components/wrapper";
import { Source } from "@/app/interfaces/source";
import { BookText } from "lucide-react";
import { FC } from "react";
import Locale from "../locales";

type SourceItemProps = {
  source: Source;
  index: number;
};

const SourceItem: FC<SourceItemProps> = ({
  source,
  index,
}) => {
  const { id, name, url } = source;
  const domain = new URL(url).hostname;

  return (
    <div
      className="relative text-xs py-3 px-3 bg-zinc-100 hover:bg-zinc-200 rounded-lg flex flex-col gap-2"
      key={id}
    >
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="absolute inset-0"
      />
      <div className="font-medium text-zinc-950 text-ellipsis overflow-hidden whitespace-nowrap break-words">
        {name}
      </div>
      <div className="flex gap-2 items-center">
        <div className="flex-1 overflow-hidden">
          <div className="text-ellipsis whitespace-nowrap break-all text-zinc-400 overflow-hidden w-full">
            {index + 1} - {domain}
          </div>
        </div>
        <div className="flex-none flex items-center">
          <Image
            className="h-3 w-3"
            alt={domain}
            src={`https://www.google.com/s2/favicons?domain=${domain}&sz=${16}`}
            width={12}
            height={12}
            unoptimized
            priority={false}
          />
        </div>
      </div>
    </div>
  );
};

export const Sources: FC<{ sources: Source[] }> = ({ sources }) => {
  return (
    <Wrapper
      title={
        <>
          <BookText /> {Locale.Sources.sources}
        </>
      }
      content={
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {sources.length > 0 ? (
            sources.map((item, index) => (
              <SourceItem key={item.id} index={index} source={item} />
            ))
          ) : (
            <>
              <Skeleton className="max-w-sm h-16 bg-zinc-200/80" />
              <Skeleton className="max-w-sm h-16 bg-zinc-200/80" />
              <Skeleton className="max-w-sm h-16 bg-zinc-200/80" />
              <Skeleton className="max-w-sm h-16 bg-zinc-200/80" />
            </>
          )}
        </div>
      }
    />
  );
};
