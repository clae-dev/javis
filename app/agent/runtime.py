"""실행 시점에 조립되는 그래프를 담는 홀더.

체크포인터(특히 AsyncPostgresSaver)는 앱 수명 동안 살아있는 커넥션이 필요해
import 시점이 아니라 lifespan 에서 그래프를 빌드한다.
"""


class Runtime:
    def __init__(self) -> None:
        self.graph = None


runtime = Runtime()
