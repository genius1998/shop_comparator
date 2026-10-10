import os
import uuid

def get_device_id(filename="device_id.txt"):
    """파일에서 device_id를 읽어오거나, 없으면 생성하여 저장하고 반환합니다."""
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            device_id = f.read().strip()
            if device_id:
                return device_id
    
    # 파일이 없거나 내용이 비어있으면 새로 생성
    device_id = f"g:{uuid.uuid4()}"
    with open(filename, 'w') as f:
        f.write(device_id)
    return device_id

if __name__ == '__main__':
    # 테스트용 코드
    did = get_device_id()
    print(f"Device ID: {did}")
    # 다시 호출해도 같은 ID가 나와야 함
    did2 = get_device_id()
    print(f"Device ID (again): {did2}")
    assert did == did2
